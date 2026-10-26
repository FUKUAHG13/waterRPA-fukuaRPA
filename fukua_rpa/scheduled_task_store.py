"""Atomic, size-bounded JSON persistence for scheduled tasks and run history.

Follows the app's defensive JSON conventions: duplicate-key rejection, a file
size cap, and atomic write via a temp file + ``os.replace``. Run history keeps
only references and result summaries (never the full script, variable values or
log body) per design §4.7.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any

from .io_utils import atomic_write_json  # noqa: F401 – re-exported for callers/tests
from .json_safety import loads_bounded_json
from .scheduled_task_model import (
    SCHEDULE_SCHEMA_VERSION,
    ScheduledRunHistory,
    ScheduledTask,
)

MAX_TASKS_FILE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_FILE_BYTES = 4 * 1024 * 1024
MAX_HISTORY_ENTRIES = 200


def _trim_history(
    history: list[ScheduledRunHistory],
) -> list[ScheduledRunHistory]:
    """Trim *history* to ``MAX_HISTORY_ENTRIES`` while keeping每任务最新记录。

    FR-20261008-501: a purely global window lets unrelated tasks evict a one-shot
    (SINGLE) task's terminal record. ``ScheduledTaskService._evaluate_due`` reads
    ``history_for(schedule_id)`` and treats "no record with an outcome/reason" as
    "never ran", so once the terminal record was evicted the trigger fired again —
    a one-shot action re-executed. Protect the newest record of every
    ``schedule_id`` so a terminal outcome stays visible while the window covers
    anything at all, and bound the result strictly to ``MAX_HISTORY_ENTRIES``.

    Insertion order is preserved. When there are more distinct tasks than the
    window allows, only the newest ``MAX_HISTORY_ENTRIES`` protected records are
    kept (the global bound still wins).
    """
    if len(history) <= MAX_HISTORY_ENTRIES:
        return history
    newest_by_schedule: dict[str, int] = {}
    for index, item in enumerate(history):
        newest_by_schedule[item.schedule_id] = index
    protected = set(newest_by_schedule.values())
    if len(protected) >= MAX_HISTORY_ENTRIES:
        return [history[index] for index in sorted(protected)[-MAX_HISTORY_ENTRIES:]]
    keep = set(protected)
    remaining = MAX_HISTORY_ENTRIES - len(keep)
    for index in range(len(history) - 1, -1, -1):
        if remaining <= 0:
            break
        if index not in keep:
            keep.add(index)
            remaining -= 1
    return [item for index, item in enumerate(history) if index in keep]


class DuplicateKeyError(ValueError):
    """Raised when a JSON object contains a duplicate key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, object]:
    """JSON ``object_pairs_hook`` that raises on any duplicate key.

    Args:
        pairs: Key/value pairs of a single JSON object in document order.

    Returns:
        Dict built from the pairs.

    Raises:
        DuplicateKeyError: If a key appears more than once in the object.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


# 这些持续时间字段必须是有限数字。loads_bounded_json 已拒绝任何 NaN/Infinity
# 数字字面量，但此处额外做类型校验，防止字符串/布尔等被当作合法时长。
_FINITE_SECONDS_KEYS = ("interval_seconds", "max_runtime_seconds", "startup_delay_seconds")


def _validate_finite_seconds(parsed: Any) -> None:
    """校验计划任务中的时长字段为有限数字（若存在）。"""
    if not isinstance(parsed, dict):
        return
    tasks = parsed.get("tasks")
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for key in _FINITE_SECONDS_KEYS:
            if key not in task:
                continue
            value = task[key]
            # None 是“未设置”的合法哨兵值（模型会将其序列化进 JSON），跳过。
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"计划字段 {key} 必须是数字：{value!r}")
            if not math.isfinite(value):
                raise ValueError(f"计划字段 {key} 包含 NaN 或无穷大：{value!r}")
            # 时长为有限负数在数学上"合法"，但语义上无意义（间隔/上限/延迟都不能
            # 为负），且会让下游定时器立即触发或永不触发。与 NaN 同口径拒绝。
            if value < 0:
                raise ValueError(f"计划字段 {key} 不能为负数：{value!r}")
        # interval_seconds/startup_delay_seconds 实际嵌套在 task["trigger"] 下，
        # 顶层键校验永远跳过它们，这里递归进入 trigger 子对象补充校验。
        trigger = task.get("trigger")
        if isinstance(trigger, dict):
            for key in ("interval_seconds", "startup_delay_seconds"):
                value = trigger.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"trigger.{key} 必须是数字，收到 {type(value).__name__}")
                if not math.isfinite(value):
                    raise ValueError(f"trigger.{key} 必须是有限数字")
                if value < 0:
                    raise ValueError(f"trigger.{key} 不能为负数：{value!r}")
            _validate_trigger_calendar_fields(trigger)


def _is_integral_number(value: Any) -> bool:
    """True for an int, or a float carrying an exact integral value.

    An integer-valued float is valid JSON and the model layer's ``_strict_int``
    accepts it, so rejecting ``1.0`` here would quarantine an entire task file as
    ``.corrupt`` for something the model is happy to take. ``bool`` is excluded even
    though it is an ``int`` subclass.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and float(value).is_integer()


def _validate_trigger_calendar_fields(trigger: dict) -> None:
    """Validate the calendar selectors that decide *when* a trigger fires.

    FR-20261003-601: same stance as the duration fields — reject the whole file
    before the model ever sees it.

    ``weekdays`` as a bare string is the shape worth guarding: iterating it yields
    characters and ``"1" in "12"`` is True, so ``"12"`` would silently register the
    wrong weekday instead of being rejected.
    """
    if "weekdays" in trigger:
        weekdays = trigger["weekdays"]
        if not isinstance(weekdays, list):
            raise ValueError(
                f"trigger.weekdays 必须是列表，收到 {type(weekdays).__name__}"
            )
        for day in weekdays:
            if not _is_integral_number(day):
                raise ValueError(
                    f"trigger.weekdays 元素必须是整数，收到 {type(day).__name__}"
                )
            if not 0 <= day <= 6:
                raise ValueError(f"trigger.weekdays 元素必须在 0~6 之间：{day!r}")
    if "day_of_month" in trigger:
        day_of_month = trigger["day_of_month"]
        if not _is_integral_number(day_of_month):
            raise ValueError(
                f"trigger.day_of_month 必须是整数，收到 {type(day_of_month).__name__}"
            )
        if not 1 <= day_of_month <= 31:
            raise ValueError(f"trigger.day_of_month 必须在 1~31 之间：{day_of_month!r}")


def safe_load_json(path: str, max_bytes: int = MAX_TASKS_FILE_BYTES) -> Any | None:
    """Load and defensively validate a scheduled-task JSON file.

    Enforces a file-size cap, rejects duplicate keys (preserving this module's
    DuplicateKeyError contract), applies the bounded JSON parser (no NaN or
    Infinity, depth/node limits), and verifies that duration fields are finite
    numbers.

    Args:
        path: Filesystem path to the JSON file.
        max_bytes: Maximum allowed file size in bytes.

    Returns:
        Parsed JSON value, or None when the file does not exist.

    Raises:
        ValueError: If the file is oversized, contains duplicate keys, fails the
            bounded parse, or has a non-finite duration field.
        OSError: If the file cannot be read.
    """
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"JSON file too large: {size} > {max_bytes} ({path})")
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    # Duplicate-key pre-pass preserves this module's DuplicateKeyError contract
    # (a ValueError subclass) for callers/tests that assert it specifically.
    try:
        json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError as exc:
        # SAFETY: JSON 解析器的递归下降在超深嵌套时抛 RecursionError（RuntimeError
        # 子类），不在 load() 的 (ValueError, OSError) 捕获范围内；转为 ValueError
        # 以进入既有损坏自愈路径（.corrupt 备份 + 空状态启动，审查 SEC-FR-001）。
        raise ValueError(f"JSON nesting too deep to parse: {path}") from exc
    # Bounded parse rejects NaN/Infinity and enforces depth/node limits.
    parsed = loads_bounded_json(raw, max_bytes=max_bytes, label="计划任务 JSON")
    # 额外校验时长字段为有限数字（防御性，覆盖 loads_bounded_json 之外的类型问题）。
    _validate_finite_seconds(parsed)
    return parsed


def _preserve_corrupt_file(path: str) -> None:
    # SAFETY: 加载失败时先把损坏的原文件原子重命名为带时间戳的 .corrupt 备份，
    # 再以空状态继续。否则随后的 save() 会用空内容覆盖磁盘上仍保留的原始数据，
    # 造成无备份的数据丢失（参考 config_store.preserve_corrupt_config 模式）。
    try:
        if not os.path.isfile(path):
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        os.replace(path, f"{path}.corrupt.{timestamp}")
    except OSError:
        # 备份失败不应阻断启动；记录后继续以空状态工作。
        import logging
        logging.getLogger(__name__).warning(
            "无法备份损坏的计划任务文件: %s", path
        )


class ScheduledTaskStore:
    """Thread-safe persistence for scheduled tasks and bounded run history."""

    def __init__(self, tasks_path: str, history_path: str):
        """Open (or create) the store backed by two JSON files and load them.

        Args:
            tasks_path: Path to the scheduled-task definitions file.
            history_path: Path to the bounded run-history file.
        """
        self._tasks_path = tasks_path
        self._history_path = history_path
        self._lock = threading.RLock()
        self._tasks: dict[str, ScheduledTask] = {}
        self._history: list[ScheduledRunHistory] = []
        # FR-b01-001: 本次加载若拒绝未来 schema 文件，任何后续保存都不得覆盖原文件
        # （只读保护）。每次 load 重新评估。
        self._future_version_rejected = False
        self.load()

    # ----- loading -----
    def load(self) -> None:
        """(Re)load tasks and history from disk into memory.

        Corrupt files are atomically backed up to a timestamped ``.corrupt``
        sibling and replaced with empty state so a later save cannot overwrite
        the original data. Individual malformed records are skipped.
        """
        with self._lock:
            self._tasks = {}
            self._future_version_rejected = False
            try:
                raw = safe_load_json(self._tasks_path, MAX_TASKS_FILE_BYTES)
            except (ValueError, OSError) as exc:
                import logging
                logging.getLogger(__name__).error(
                    "计划任务文件损坏，以空配置启动: %s", exc
                )
                _preserve_corrupt_file(self._tasks_path)
                raw = {"tasks": []}
            # FR-20260812-D2-001: 校验 schema_version——未来格式文件不得被部分
            # 加载后在下次编辑时静默降级重写（与主配置存储拒绝路径一致）。版本
            # 超前时明确拒绝并进入空状态，保存路径仍写当前版本（不覆盖原文件）。
            # FR-20261003-602: 根容器必须是对象。``safe_load_json`` 对 ``[1,2,3]``
            # 或 ``5`` 这类合法 JSON 返回 list/int，随后 ``raw.get`` 抛
            # AttributeError——而该异常不在上面的 ``except (ValueError, OSError)``
            # 捕获范围内，会直接逃出 load() 让应用启动失败。按损坏文件处理。
            if raw and not isinstance(raw, dict):
                import logging

                logging.getLogger(__name__).error(
                    "计划任务文件根节点不是对象（收到 %s），以空配置启动",
                    type(raw).__name__,
                )
                _preserve_corrupt_file(self._tasks_path)
                raw = {"tasks": []}
            if raw:
                raw_version = raw.get("schema_version")
                if (
                    isinstance(raw_version, (int, float))
                    and int(raw_version) > SCHEDULE_SCHEMA_VERSION
                ):
                    import logging
                    logging.getLogger(__name__).error(
                        "计划任务文件 schema_version=%s 高于当前支持 %s，"
                        "已拒绝加载（原文件保留，后续保存被禁止以免降级覆盖）",
                        raw_version,
                        SCHEDULE_SCHEMA_VERSION,
                    )
                    # FR-b01-001: 标记只读保护，_save_tasks 据此跳过覆盖。
                    self._future_version_rejected = True
                    raw = {"tasks": []}
                # FR-20261003-602: 容器类型必须校验，不能只靠 ``or []`` 兜住
                # 空值。``{"tasks": "just-a-string"}`` 会让 ``for`` 逐**字符**迭代，
                # 每个字符都走一次 from_dict 失败后被 ``continue`` 吞掉——文件被
                # 静默清空却不留 .corrupt 备份，用户无法自助恢复。非列表根容器
                # 按损坏文件处理，与 JSON 解析失败同路径。
                items = raw.get("tasks", [])
                if not isinstance(items, list):
                    import logging

                    logging.getLogger(__name__).error(
                        "计划任务文件 tasks 字段不是列表（收到 %s），以空配置启动",
                        type(items).__name__,
                    )
                    _preserve_corrupt_file(self._tasks_path)
                    items = []
                for item in items:
                    try:
                        task = ScheduledTask.from_dict(item)
                    except (KeyError, ValueError, TypeError):
                        continue
                    self._tasks[task.schedule_id] = task
            self._history = []
            try:
                raw_h = safe_load_json(self._history_path, MAX_HISTORY_FILE_BYTES)
            except (ValueError, OSError) as exc:
                import logging
                logging.getLogger(__name__).error(
                    "计划任务历史文件损坏，以空历史启动: %s", exc
                )
                _preserve_corrupt_file(self._history_path)
                raw_h = {"history": []}
            if raw_h and not isinstance(raw_h, dict):
                import logging

                logging.getLogger(__name__).error(
                    "计划任务历史文件根节点不是对象（收到 %s），以空历史启动",
                    type(raw_h).__name__,
                )
                _preserve_corrupt_file(self._history_path)
                raw_h = {"history": []}
            if raw_h:
                # FR-20261003-602: 同 tasks——非列表的 history 容器必须走损坏
                # 路径，不能让 for 逐字符迭代后静默清空。
                history_items = raw_h.get("history", [])
                if not isinstance(history_items, list):
                    import logging

                    logging.getLogger(__name__).error(
                        "计划任务历史文件 history 字段不是列表（收到 %s），以空历史启动",
                        type(history_items).__name__,
                    )
                    _preserve_corrupt_file(self._history_path)
                    history_items = []
                for item in history_items:
                    try:
                        self._history.append(ScheduledRunHistory.from_dict(item))
                    except (KeyError, ValueError, TypeError):
                        continue

    # ----- saving -----
    def _save_tasks(self) -> None:
        """Persist only the tasks file. Caller must hold self._lock."""
        if self._future_version_rejected:
            # FR-b01-001: 加载曾被拒绝的未来 schema 文件时，任何保存都不得把原文件
            # 降级覆盖（数据丢失）。内存编辑仅限当前会话；待用户升级软件或处理该文件
            # 后重新加载才会恢复持久化。
            import logging
            logging.getLogger(__name__).warning(
                "计划任务文件 schema 超前，跳过保存以保护原文件（%s）",
                self._tasks_path,
            )
            return
        data = {
            "schema_version": SCHEDULE_SCHEMA_VERSION,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        atomic_write_json(self._tasks_path, data)

    def _save_history(self) -> None:
        """Persist only the history file. Caller must hold self._lock."""
        history_data = {
            "history": [h.to_dict() for h in _trim_history(self._history)]
        }
        atomic_write_json(self._history_path, history_data)

    def save(self) -> None:
        """Atomically rewrite both the tasks and history files from memory."""
        # DESIGN: 每次变更完整重写 JSON。当前计划任务数量极少（通常 < 20），
        # 全量重写的 I/O 开销可忽略。若未来任务数量增长，可改为增量写入。
        with self._lock:
            self._save_tasks()
            self._save_history()

    # ----- queries -----
    def all(self) -> list[ScheduledTask]:
        """Return a snapshot list of every stored task."""
        with self._lock:
            return list(self._tasks.values())

    def get(self, schedule_id: str) -> ScheduledTask | None:
        """Return the task with the given id, or None if absent."""
        with self._lock:
            return self._tasks.get(schedule_id)

    def enabled(self) -> list[ScheduledTask]:
        """Return a snapshot list of only the enabled tasks."""
        with self._lock:
            return [t for t in self._tasks.values() if t.enabled]

    # ----- mutations (each persists immediately) -----
    def add(self, task: ScheduledTask) -> None:
        """Insert or replace a task by id and persist the tasks file."""
        with self._lock:
            self._tasks[task.schedule_id] = task
            self._save_tasks()

    def add_many(self, tasks: list[ScheduledTask]) -> None:
        """FR-20260812-D13-003: 批量插入任务并只持久化一次。

        导入路径必须整体落盘：逐条 ``add`` 会在中途失败时留下"部分导入"且
        每次全量写盘。调用方先完成全部解析/校验/冲突再生，再一次性批量写入
        （一次 ``_save_tasks`` 落盘）。注意（独立审查 2026-08-12）：落盘
        ``_save_tasks`` 抛异常（如磁盘满）时内存 ``_tasks`` 已插入，没有回滚；
        调用方已在写入前完成全部校验，磁盘故障属极端环境，未做回滚属已知边界。
        """
        if not tasks:
            return
        with self._lock:
            for task in tasks:
                self._tasks[task.schedule_id] = task
            self._save_tasks()

    def update(self, task: ScheduledTask) -> None:
        """Replace the stored task matching ``task.schedule_id`` and persist."""
        with self._lock:
            self._tasks[task.schedule_id] = task
            self._save_tasks()

    def remove(self, schedule_id: str) -> None:
        """Delete a task by id (no-op if absent) and persist."""
        with self._lock:
            self._tasks.pop(schedule_id, None)
            self._save_tasks()

    def set_enabled(self, schedule_id: str, enabled: bool) -> None:
        """Toggle a task's enabled flag and persist (no-op if absent)."""
        with self._lock:
            task = self._tasks.get(schedule_id)
            if task is None:
                return
            task.enabled = enabled
            self._save_tasks()

    def set_enabled_many(self, schedule_ids: list[str], enabled: bool) -> None:
        """FR-20260812 建议-架构-5: 批量切换 enabled 并只持久化一次。

        多选行批量启停时避免每条触发一次全量写盘。
        """
        if not schedule_ids:
            return
        with self._lock:
            for schedule_id in schedule_ids:
                task = self._tasks.get(schedule_id)
                if task is None:
                    continue
                task.enabled = enabled
            self._save_tasks()

    def remove_many(self, schedule_ids: list[str]) -> None:
        """FR-20260812 建议-架构-5: 批量删除并只持久化一次。"""
        if not schedule_ids:
            return
        with self._lock:
            for schedule_id in schedule_ids:
                self._tasks.pop(schedule_id, None)
            self._save_tasks()

    # ----- history -----
    def add_history(self, record: ScheduledRunHistory) -> None:
        """Append a run-history record, trimming to the bounded window, and persist."""
        with self._lock:
            self._history.append(record)
            if len(self._history) > MAX_HISTORY_ENTRIES:
                self._history = _trim_history(self._history)
            self._save_history()

    def history_for(self, schedule_id: str) -> list[ScheduledRunHistory]:
        """Return all history records for one task, in insertion order."""
        with self._lock:
            return [h for h in self._history if h.schedule_id == schedule_id]

    def clear_history(self) -> None:
        """Delete every history record and persist the empty history file."""
        with self._lock:
            self._history = []
            self._save_history()

    def all_history(self) -> list[ScheduledRunHistory]:
        """Return a snapshot list of every stored history record."""
        with self._lock:
            return list(self._history)
