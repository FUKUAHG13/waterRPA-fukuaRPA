"""Atomic persistence tests for :class:`fukua_rpa.scheduled_task_store`."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fukua_rpa.scheduled_task_model import (
    MISSED_CATCHUP_ONCE,
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTrigger,
    TriggerKind,
    new_schedule_id,
)
from fukua_rpa.scheduled_task_store import (
    MAX_HISTORY_ENTRIES,
    DuplicateKeyError,
    ScheduledTaskStore,
    _validate_finite_seconds,
    safe_load_json,
)


def _make_task(name="t", enabled=True, profile_id="p1"):
    return ScheduledTask(
        schedule_id=new_schedule_id(),
        name=name,
        profile_id=profile_id,
        profile_name_snapshot="P1",
        enabled=enabled,
        trigger=ScheduledTrigger(kind=TriggerKind.DAILY, local_time=datetime.time(9, 0)),
        missed_policy=MISSED_CATCHUP_ONCE,
    )


class TestScheduledTaskStore(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self._tmp_dir, "scheduled_tasks.json")
        self.history_path = os.path.join(self._tmp_dir, "scheduled_task_history.json")
        self.store = ScheduledTaskStore(self.tasks_path, self.history_path)

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_add_get_all(self):
        task = _make_task()
        self.store.add(task)
        self.assertIsNotNone(self.store.get(task.schedule_id))
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.enabled()[0].schedule_id, task.schedule_id)

    def test_add_many_persists_atomically_once(self):
        """FR-20260812-D13-003: add_many 批量插入且只持久化一次（原子导入）。"""
        tasks = [_make_task() for _ in range(3)]
        for i, task in enumerate(tasks):
            task.schedule_id = f"s-{i}"
        real_save = self.store._save_tasks
        calls = []

        def _spy_save():
            calls.append(1)
            real_save()

        self.store._save_tasks = _spy_save
        self.store.add_many(tasks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.all()), 3)
        # 落盘后可重载
        reloaded = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(len(reloaded.all()), 3)

    def test_add_many_empty_is_noop(self):
        with mock.patch.object(self.store, "_save_tasks") as save:
            self.store.add_many([])
            save.assert_not_called()

    def test_set_enabled_many_persists_atomically_once(self):
        """FR-20260812 建议-架构-5: store 批量启停只持久化一次。"""
        tasks = [_make_task() for _ in range(3)]
        for i, task in enumerate(tasks):
            task.schedule_id = f"s-{i}"
        self.store.add_many(tasks)
        calls = []
        real_save = self.store._save_tasks

        def _spy_save():
            calls.append(1)
            real_save()

        self.store._save_tasks = _spy_save
        self.store.set_enabled_many(["s-0", "s-1"], False)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.store.get("s-0").enabled)
        self.assertFalse(self.store.get("s-1").enabled)
        self.assertTrue(self.store.get("s-2").enabled)

    def test_remove_many_persists_atomically_once(self):
        """FR-20260812 建议-架构-5: store 批量删除只持久化一次。"""
        tasks = [_make_task() for _ in range(3)]
        for i, task in enumerate(tasks):
            task.schedule_id = f"s-{i}"
        self.store.add_many(tasks)
        calls = []
        real_save = self.store._save_tasks

        def _spy_save():
            calls.append(1)
            real_save()

        self.store._save_tasks = _spy_save
        self.store.remove_many(["s-0", "s-2"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.store.get("s-0"))
        self.assertIsNotNone(self.store.get("s-1"))
        self.assertIsNone(self.store.get("s-2"))

    def test_future_schema_version_rejected_without_overwrite(self):
        """FR-20260812-D2-001: schema_version 超前的计划文件必须拒绝加载
        （只读保护），不得部分加载后在下次编辑时静默降级重写。"""
        import json as _json

        task = _make_task()
        self.store.add(task)
        # 手工写入未来版本
        data = {
            "schema_version": 999,
            "tasks": [t.to_dict() for t in self.store.all()],
        }
        with open(self.tasks_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, ensure_ascii=False)
        reloaded = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(reloaded.all(), [])
        # 原文件未被覆盖（仍含未来版本与原始任务）
        with open(self.tasks_path, encoding="utf-8") as fh:
            on_disk = _json.load(fh)
        self.assertEqual(on_disk["schema_version"], 999)
        self.assertEqual(len(on_disk["tasks"]), 1)

    def test_future_schema_version_not_overwritten_by_later_save(self):
        """FR-b01-001: 未来 schema 文件在 reload 之后的 save 路径也不得被降级覆盖。"""
        import json as _json

        task = _make_task()
        self.store.add(task)
        data = {
            "schema_version": 999,
            "tasks": [t.to_dict() for t in self.store.all()],
        }
        with open(self.tasks_path, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, ensure_ascii=False)
        reloaded = ScheduledTaskStore(self.tasks_path, self.history_path)
        # 未来版本被拒绝后，任意写操作不得把原文件降级重写（内存编辑仅限当前会话）。
        reloaded.add(_make_task(name="future-only"))
        with open(self.tasks_path, encoding="utf-8") as fh:
            on_disk = _json.load(fh)
        self.assertEqual(on_disk["schema_version"], 999)
        self.assertEqual(len(on_disk["tasks"]), 1)

    def test_update_persists(self):
        task = _make_task(enabled=True)
        self.store.add(task)
        task.enabled = False
        self.store.update(task)
        self.assertFalse(self.store.get(task.schedule_id).enabled)
        self.assertEqual(self.store.get(task.schedule_id).missed_policy, MISSED_CATCHUP_ONCE)

    def test_set_enabled(self):
        task = _make_task(enabled=True)
        self.store.add(task)
        self.store.set_enabled(task.schedule_id, False)
        self.assertFalse(self.store.get(task.schedule_id).enabled)
        self.assertEqual(self.store.enabled(), [])

    def test_remove(self):
        task = _make_task()
        self.store.add(task)
        self.store.remove(task.schedule_id)
        self.assertIsNone(self.store.get(task.schedule_id))
        self.assertEqual(self.store.all(), [])

    def test_reload_from_disk(self):
        task = _make_task()
        self.store.add(task)

        # A fresh store on the same files must see the persisted task.
        store2 = ScheduledTaskStore(self.tasks_path, self.history_path)
        loaded = store2.get(task.schedule_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, task.name)
        self.assertEqual(loaded.missed_policy, MISSED_CATCHUP_ONCE)

    def test_deep_nested_corrupt_tasks_file_is_preserved_and_boots_empty(self):
        # SEC-FR-001：超深嵌套损坏文件曾以 RecursionError 逃逸 (ValueError, OSError)
        # 捕获，绕过 .corrupt 自愈并沿服务构造中断启动；必须按损坏文件路径处理：
        # 原文件备份为 .corrupt 并以空状态启动。
        with open(self.tasks_path, "w", encoding="utf-8") as handle:
            handle.write("[" * 60000)
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(store.all(), [])
        corrupt = [
            name
            for name in os.listdir(self._tmp_dir)
            if name.startswith("scheduled_tasks.json.corrupt")
        ]
        self.assertEqual(len(corrupt), 1)

    def test_non_object_root_tasks_file_is_preserved_and_boots_empty(self):
        # FR-002：合法但非对象的 JSON 根（如 "[1, 2, 3]"）能通过有界解析，却会让
        # load() 的 raw.get(...) 抛 AttributeError；该异常不在 (ValueError, OSError)
        # 捕获范围内，会逃逸到窗口构造并阻断启动，同时跳过 .corrupt 自愈备份。
        # 必须按损坏文件路径处理：原文件备份为 .corrupt 并以空状态启动。
        for literal in ("[1, 2, 3]", '"just-a-string"', "5"):
            with self.subTest(tasks_root=literal):
                with open(self.tasks_path, "w", encoding="utf-8") as handle:
                    handle.write(literal)
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.all(), [])
                corrupt = [
                    name
                    for name in os.listdir(self._tmp_dir)
                    if name.startswith("scheduled_tasks.json.corrupt")
                ]
                self.assertEqual(len(corrupt), 1, literal)
                for name in corrupt:
                    os.remove(os.path.join(self._tmp_dir, name))

    def test_non_object_root_history_file_is_preserved_and_boots_empty(self):
        # FR-002：历史文件根为非对象时同样必须走损坏自愈路径。
        for literal in ("[1, 2, 3]", '"x"', "5"):
            with self.subTest(history_root=literal):
                with open(self.history_path, "w", encoding="utf-8") as handle:
                    handle.write(literal)
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.history_for("nope"), [])
                corrupt = [
                    name
                    for name in os.listdir(self._tmp_dir)
                    if name.startswith("scheduled_task_history.json.corrupt")
                ]
                self.assertEqual(len(corrupt), 1, literal)
                for name in corrupt:
                    os.remove(os.path.join(self._tmp_dir, name))

    def test_non_list_tasks_value_is_preserved_and_boots_empty(self):
        # FR-SCOPE006-002：根是对象但 tasks 为**非可迭代值**（5 / 1.5 / true）时，
        # `for item in raw.get("tasks", []) or []` 位于 try 之外，抛
        # `TypeError: 'int' object is not iterable` 并逃逸到窗口构造，阻断应用启动，
        # 且跳过 .corrupt 自愈备份（用户无法自助恢复原文件）。
        # 必须按损坏文件路径处理：原文件备份为 .corrupt 并以空状态启动。
        for literal in ("5", "1.5", "true"):
            with self.subTest(tasks_value=literal):
                with open(self.tasks_path, "w", encoding="utf-8") as handle:
                    handle.write('{"tasks": ' + literal + "}")
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.all(), [])
                corrupt = [
                    name
                    for name in os.listdir(self._tmp_dir)
                    if name.startswith("scheduled_tasks.json.corrupt")
                ]
                self.assertEqual(len(corrupt), 1, literal)
                for name in corrupt:
                    os.remove(os.path.join(self._tmp_dir, name))

    def test_non_list_history_value_is_preserved_and_boots_empty(self):
        # FR-SCOPE006-002：history 同理，非可迭代值必须走损坏自愈路径。
        for literal in ("5", "1.5", "true"):
            with self.subTest(history_value=literal):
                with open(self.history_path, "w", encoding="utf-8") as handle:
                    handle.write('{"history": ' + literal + "}")
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.history_for("nope"), [])
                corrupt = [
                    name
                    for name in os.listdir(self._tmp_dir)
                    if name.startswith("scheduled_task_history.json.corrupt")
                ]
                self.assertEqual(len(corrupt), 1, literal)
                for name in corrupt:
                    os.remove(os.path.join(self._tmp_dir, name))

    def test_empty_containers_are_not_treated_as_corrupt(self):
        # 必须保留行为：合法的空容器（根 `{}`、`[]` 值的 tasks/history）不得被误判为
        # 损坏，也不得产生 .corrupt 备份。
        for literal in ("{}", '{"tasks": []}', '{"tasks": [], "history": []}'):
            with self.subTest(root=literal):
                with open(self.tasks_path, "w", encoding="utf-8") as handle:
                    handle.write(literal)
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.all(), [])
                corrupt = [
                    name
                    for name in os.listdir(self._tmp_dir)
                    if ".corrupt" in name
                ]
                self.assertEqual(corrupt, [], literal)

    def test_string_and_object_tasks_values_do_not_crash(self):
        # 必须保留行为：str / dict 的既有优雅降级不得回退为崩溃。
        # 注意：本用例只断言「不抛异常且以空状态启动」，**不**断言备份——
        # 修复前 str/dict 本就不生成 .corrupt（会被 from_dict 的 TypeError 吞掉），
        # 用备份作判据会写成假阳性测试。
        for literal in ('"oops"', '{"a": 1}'):
            with self.subTest(tasks_value=literal):
                with open(self.tasks_path, "w", encoding="utf-8") as handle:
                    handle.write('{"tasks": ' + literal + "}")
                store = ScheduledTaskStore(self.tasks_path, self.history_path)
                self.assertEqual(store.all(), [])

    def test_history_add_and_query(self):
        task = _make_task()
        self.store.add(task)
        hist = ScheduledRunHistory(
            history_id="h1",
            schedule_id=task.schedule_id,
            run_id="r1",
            profile_id=task.profile_id,
            due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
            outcome="success",
        )
        self.store.add_history(hist)
        self.assertEqual(len(self.store.history_for(task.schedule_id)), 1)
        self.assertEqual(self.store.all_history()[0].outcome, "success")

    def test_history_bounded(self):
        for i in range(205):
            self.store.add_history(
                ScheduledRunHistory(
                    history_id=f"h{i}",
                    schedule_id="s",
                    run_id=f"r{i}",
                    profile_id="p",
                    due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
                )
            )
        # Cap is 200; oldest entries should be trimmed.
        self.assertEqual(len(self.store.all_history()), 200)
        ids = {h.history_id for h in self.store.all_history()}
        self.assertNotIn("h0", ids)
        self.assertIn("h204", ids)

    def test_history_keeps_each_task_newest_record_under_global_pressure(self):
        """FR-20261008-501：无关任务的历史不得挤掉某任务的最新终局记录。

        纯全局窗口会让一次性（SINGLE）任务的终局记录被挤出，服务随即按
        「没有任何带 outcome/reason 的记录」判其从未运行并重复触发。这里证明
        最新记录按 ``schedule_id`` 受保护，且结果仍严格受全局上限约束。
        """
        self.store.add_history(
            ScheduledRunHistory(
                history_id="terminal",
                schedule_id="one-shot",
                run_id="r0",
                profile_id="p",
                due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
                outcome="success",
            )
        )
        for index in range(MAX_HISTORY_ENTRIES + 50):
            self.store.add_history(
                ScheduledRunHistory(
                    history_id=f"noise{index}",
                    schedule_id="noisy",
                    run_id=f"r{index}",
                    profile_id="p",
                    due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
                )
            )
        self.assertEqual(len(self.store.all_history()), MAX_HISTORY_ENTRIES)
        kept = self.store.history_for("one-shot")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].outcome, "success")

    def test_history_global_bound_wins_over_protection_under_extreme_pressure(self):
        """FR-RISK-20261008-902：>MAX 个不同计划时，全局上限仍胜出。

        本用例**记录当前已文档化的取舍**（``_trim_history`` docstring：
        "the global bound still wins"），不判定其为期望行为。当历史中累计
        超过 ``MAX_HISTORY_ENTRIES`` 个不同 ``schedule_id`` 时，最早批次的
        受保护终态记录（含启用中 SINGLE 任务）会被逐出，服务可能把该任务
        判为「从未运行」并重复触发。维护者待裁定（06_改进路线 §5-2）：
        若决定提高保护优先级，本用例断言应随之反转并同步实现。
        """
        self.store.add_history(
            ScheduledRunHistory(
                history_id="early-terminal",
                schedule_id="one-shot-early",
                run_id="r0",
                profile_id="p",
                due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
                outcome="success",
            )
        )
        for index in range(MAX_HISTORY_ENTRIES + 10):
            self.store.add_history(
                ScheduledRunHistory(
                    history_id=f"plan{index}",
                    schedule_id=f"distinct-{index}",
                    run_id=f"r{index}",
                    profile_id="p",
                    due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
                )
            )
        self.assertEqual(len(self.store.all_history()), MAX_HISTORY_ENTRIES)
        kept_ids = {h.schedule_id for h in self.store.all_history()}
        # 当前行为：最早的终态记录被逐出（全局上限胜出）。
        self.assertNotIn("one-shot-early", kept_ids)
        # 最新 200 个受保护记录幸存。
        self.assertIn("distinct-209", kept_ids)
        self.assertNotIn("distinct-0", kept_ids)

    def test_clear_history(self):
        task = _make_task()
        self.store.add(task)
        self.store.add_history(
            ScheduledRunHistory(
                history_id="h1",
                schedule_id=task.schedule_id,
                run_id="r1",
                profile_id="p",
                due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
            )
        )
        self.store.clear_history()
        self.assertEqual(self.store.all_history(), [])


class TestSafeLoadJson(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_duplicate_key_rejected(self):
        path = os.path.join(self._tmp_dir, "dup.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a": 1, "a": 2}')
        with self.assertRaises(DuplicateKeyError):
            safe_load_json(path)

    def test_missing_file_loads_empty(self):
        result = safe_load_json(os.path.join(self._tmp_dir, "nope.json"))
        self.assertIsNone(result)

    def test_oversized_file_rejected(self):
        path = os.path.join(self._tmp_dir, "big.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        # Force an artificially tiny cap.
        with self.assertRaises(ValueError):
            safe_load_json(path, max_bytes=1)


class ModelLevelNegativeDurationTests(unittest.TestCase):
    """REG-20261006-001: 负时长必须在**模型层**被拒，覆盖非 load() 入口。

    修复前负值防御只在``_validate_finite_seconds``（由 ``load()`` 调用），
    ``import_tasks`` 等直接走 ``ScheduledTask.from_dict`` 的入口可把负值带进
    运行期：负 interval_seconds 使任务静默不触发且状态不进入 INVALID_CONFIG；
    负 max_runtime_seconds 传到 QTimer.start 会立即触发看门狗超时。
    """

    BASE = {
        "schedule_id": "reg-001",
        "name": "neg",
        "profile_id": "p1",
        "profile_name_snapshot": "P1",
        "enabled": True,
        "missed_policy": MISSED_CATCHUP_ONCE,
    }

    def test_negative_interval_seconds_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTask.from_dict(
                {
                    **self.BASE,
                    "trigger": {
                        "kind": "interval",
                        "local_time": "09:00:00",
                        "interval_seconds": -5,
                    },
                }
            )

    def test_negative_startup_delay_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTask.from_dict(
                {
                    **self.BASE,
                    "trigger": {
                        "kind": "startup",
                        "local_time": "09:00:00",
                        "startup_delay_seconds": -1,
                    },
                }
            )

    def test_negative_max_runtime_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTask.from_dict(
                {
                    **self.BASE,
                    "trigger": {"kind": "daily", "local_time": "09:00:00"},
                    "max_runtime_seconds": -10,
                }
            )

    def test_zero_and_positive_still_accepted(self):
        """反向验收：0 与正常正值不得被新防御误拒。"""
        task = ScheduledTask.from_dict(
            {
                **self.BASE,
                "trigger": {
                    "kind": "interval",
                    "local_time": "09:00:00",
                    "interval_seconds": 60,
                },
                "max_runtime_seconds": 0,
            }
        )
        self.assertEqual(task.trigger.interval_seconds, 60)
        self.assertEqual(task.max_runtime_seconds, 0)

    def test_absent_fields_default_to_none(self):
        """未设置时长字段仍为合法哨兵值（None），不得被拒。"""
        task = ScheduledTask.from_dict(
            {**self.BASE, "trigger": {"kind": "daily", "local_time": "09:00:00"}}
        )
        self.assertIsNone(task.max_runtime_seconds)
        self.assertIsNone(task.trigger.interval_seconds)


class TestValidateFiniteSeconds(unittest.TestCase):
    """_validate_finite_seconds 防御性校验计划任务的时长字段。"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _with_tasks(self, tasks):
        return {"tasks": tasks}

    def test_valid_finite_numbers_pass(self):
        _validate_finite_seconds(
            self._with_tasks([{"interval_seconds": 5, "max_runtime_seconds": 10}])
        )

    def test_none_sentinel_allowed(self):
        _validate_finite_seconds(
            self._with_tasks([{"interval_seconds": None, "max_runtime_seconds": None}])
        )

    def test_missing_key_allowed(self):
        _validate_finite_seconds(self._with_tasks([{"name": "only"}]))

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(self._with_tasks([{"interval_seconds": True}]))

    # FR-20261003-601: trigger.weekdays / trigger.day_of_month 也必须在进入
    # 模型前严格校验，与时长字段同口径（整文件拒绝 + .corrupt 隔离）。
    def test_trigger_weekdays_string_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(
                self._with_tasks([{"trigger": {"weekdays": "12"}}])
            )

    def test_trigger_weekdays_string_element_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(
                self._with_tasks([{"trigger": {"weekdays": ["1", "2"]}}])
            )

    def test_trigger_weekday_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(self._with_tasks([{"trigger": {"weekdays": [7]}}]))

    def test_trigger_day_of_month_string_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(
                self._with_tasks([{"trigger": {"day_of_month": "15"}}])
            )

    def test_trigger_day_of_month_out_of_range_rejected(self):
        for bad in (0, 32):
            with self.subTest(day=bad), self.assertRaises(ValueError):
                _validate_finite_seconds(
                    self._with_tasks([{"trigger": {"day_of_month": bad}}])
                )

    def test_trigger_valid_weekdays_and_day_pass(self):
        _validate_finite_seconds(
            self._with_tasks(
                [{"trigger": {"weekdays": [0, 6], "day_of_month": 15}}]
            )
        )

    def test_trigger_absent_fields_pass(self):
        _validate_finite_seconds(self._with_tasks([{"trigger": {"kind": "daily"}}]))

    def test_string_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(self._with_tasks([{"interval_seconds": "5"}]))

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(
                self._with_tasks([{"interval_seconds": float("nan")}])
            )

    def test_inf_rejected(self):
        with self.assertRaises(ValueError):
            _validate_finite_seconds(
                self._with_tasks([{"max_runtime_seconds": float("inf")}])
            )

    def test_non_dict_top_level_noop(self):
        _validate_finite_seconds([{"interval_seconds": True}])
        _validate_finite_seconds(None)

    def test_tasks_not_list_noop(self):
        _validate_finite_seconds({"tasks": {"interval_seconds": True}})

    def test_safe_load_json_rejects_bad_seconds(self):
        path = os.path.join(self._tmp_dir, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"tasks": [{"interval_seconds": "5"}]}))
        with self.assertRaises(ValueError):
            safe_load_json(path)

    # ------------------------------------------------------------------
    # FR-20261005-003 / FR-RISK-20261005-003: 口径一致性与正值防御
    # ------------------------------------------------------------------

    def test_integer_valued_float_weekdays_accepted(self):
        """整数值 float 是合法 JSON（模型 ``_strict_int`` 明确接受），不得拒。

        缺陷对象上此处抛 ValueError，且因该校验位于 ``load()`` 内层
        ``try: from_dict / except: continue`` 之外，整个任务文件会被隔离为
        ``.corrupt``，全部计划任务以空状态启动。
        """
        _validate_finite_seconds(
            {"tasks": [{"trigger": {"weekdays": [1.0, 2.0]}}]}
        )
        _validate_finite_seconds({"tasks": [{"trigger": {"day_of_month": 15.0}}]})

    def test_non_integer_float_and_bool_still_rejected(self):
        """反向验收：非整数 float / bool / 字符串仍须拒绝（不得放宽）。"""
        for payload in (
            {"trigger": {"weekdays": [1.5]}},
            {"trigger": {"day_of_month": 15.5}},
            {"trigger": {"weekdays": [True]}},
            {"trigger": {"day_of_month": True}},
            {"trigger": {"day_of_month": "15"}},
            {"trigger": {"weekdays": "123"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validate_finite_seconds({"tasks": [payload]})

    def test_out_of_range_still_rejected(self):
        """取值域边界不变：weekdays 0..6、day_of_month 1..31。"""
        for payload in (
            {"trigger": {"weekdays": [7]}},
            {"trigger": {"weekdays": [-1]}},
            {"trigger": {"day_of_month": 0}},
            {"trigger": {"day_of_month": 32}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validate_finite_seconds({"tasks": [payload]})

    def test_negative_duration_rejected(self):
        """负时长会使看门狗立即到点或周期任务静默不触发，须在存储层拒绝。

        ``max_runtime_seconds`` 是**任务顶层**字段（``scheduled_task_model``
        :483/:533），``interval_seconds`` / ``startup_delay_seconds`` 嵌套在
        ``trigger`` 下；两层都须拒绝负值。
        """
        for payload in (
            {"trigger": {"interval_seconds": -60}},
            {"trigger": {"startup_delay_seconds": -0.5}},
            {"max_runtime_seconds": -10},
            {"interval_seconds": -1},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validate_finite_seconds({"tasks": [payload]})

    def test_zero_and_positive_durations_accepted(self):
        """反向验收：0 与正常正值仍须被接受（不得过度拒绝）。"""
        _validate_finite_seconds(
            {"tasks": [{"trigger": {"interval_seconds": 0, "startup_delay_seconds": 0}}]}
        )
        _validate_finite_seconds(
            {"tasks": [{"trigger": {"interval_seconds": 3600, "max_runtime_seconds": 60}}]}
        )

    def test_load_keeps_tasks_when_one_has_integer_valued_float(self):
        """端到端：含 ``15.0`` 的文件应正常加载，且**不**产生 .corrupt 隔离。

        这是原缺陷的真实入口——``ScheduledTaskStore.load()``。
        """
        store_dir = os.path.join(self._tmp_dir, "store_float")
        os.makedirs(store_dir, exist_ok=True)
        tasks_path = os.path.join(store_dir, "scheduled_tasks.json")
        history_path = os.path.join(store_dir, "scheduled_task_history.json")
        good = {
            "schedule_id": new_schedule_id(),
            "name": "keep-me",
            "profile_id": "p1",
            "profile_name_snapshot": "P1",
            "enabled": True,
            "trigger": {
                "kind": "monthly",
                "local_time": "09:00:00",
                "day_of_month": 15,
            },
            "missed_policy": MISSED_CATCHUP_ONCE,
        }
        float_task = {
            **good,
            "schedule_id": new_schedule_id(),
            "name": "float-day",
            "trigger": {
                "kind": "monthly",
                "local_time": "09:00:00",
                "day_of_month": 15.0,
            },
        }
        with open(tasks_path, "w", encoding="utf-8") as handle:
            json.dump({"tasks": [good, float_task]}, handle, ensure_ascii=False)

        store = ScheduledTaskStore(tasks_path, history_path)
        store.load()
        loaded = store.all()

        self.assertEqual(
            len(loaded),
            2,
            "合法 JSON（day_of_month=15.0）不得导致整文件隔离或任务丢失",
        )
        leftovers = [n for n in os.listdir(store_dir) if ".corrupt" in n]
        self.assertEqual(leftovers, [], f"不应产生 .corrupt 隔离文件：{leftovers}")


if __name__ == "__main__":
    unittest.main()
