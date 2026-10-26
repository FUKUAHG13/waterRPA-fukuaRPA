"""Central log presets, categories, and custom filtering policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .json_safety import loads_bounded_json

LOG_CRITICAL = "critical"
LOG_RUN = "run"
LOG_STEP = "step"
LOG_FLOW = "flow"
LOG_ACTION = "action"
LOG_RECOGNITION = "recognition"
LOG_COORDINATES = "coordinates"
LOG_PARAMETERS = "parameters"
LOG_TIMING = "timing"
LOG_BACKEND = "backend"
LOG_ADAPTIVE = "adaptive"
LOG_APPLICATION = "application"
LOG_TIMESTAMP = "timestamp"


@dataclass(frozen=True)
class LogCategorySpec:
    """Describe an immutable specification for a single log category.

    Attributes:
        key: Stable identifier used in configuration and filtering.
        label: Human-readable short name shown in the UI.
        description: Longer explanation of what this category covers.
    """

    key: str
    label: str
    description: str


LOG_CATEGORY_SPECS = (
    LogCategorySpec(LOG_RUN, "运行开始、结束与循环进度", "脚本启动、结束和循环范围信息"),
    LogCategorySpec(LOG_STEP, "步骤结果", "每一步成功、失败、跳过及耗时结果"),
    LogCategorySpec(LOG_FLOW, "流程分支与跳转", "成功/失败分支、循环跳转和条件流程"),
    LogCategorySpec(LOG_ACTION, "动作细节", "鼠标、键盘、等待、提醒和变量动作"),
    LogCategorySpec(LOG_RECOGNITION, "识别结果", "目标命中、未命中、倍率和相似度"),
    LogCategorySpec(LOG_COORDINATES, "坐标与点位", "实际坐标、自定义点位和偏移状态"),
    LogCategorySpec(LOG_PARAMETERS, "运行与步骤参数", "完全调试使用的配置和步骤参数快照"),
    LogCategorySpec(LOG_TIMING, "耗时与性能", "识别、截图、匹配、点击和总体性能"),
    LogCategorySpec(LOG_BACKEND, "底层核心、缓存与回退", "原生 DLL、OpenCV、截图后端和缓存状态"),
    LogCategorySpec(LOG_ADAPTIVE, "画面唤醒与缩放记忆", "变化唤醒、自适应等待和常用倍率学习"),
    LogCategorySpec(LOG_APPLICATION, "设置、方案与按键映射", "主界面产生的设置和工具事件"),
    LogCategorySpec(LOG_TIMESTAMP, "界面本地时间", "界面日志显示毫秒时间；文件日志始终带时间"),
)

LOG_CATEGORY_KEYS = tuple(spec.key for spec in LOG_CATEGORY_SPECS)
LOG_CATEGORY_BY_KEY = {spec.key: spec for spec in LOG_CATEGORY_SPECS}

LOG_MODE_SIMPLE = "simple"
LOG_MODE_DETAILED = "detailed"
LOG_MODE_COMPLETE = "complete"
LOG_MODE_CUSTOM = "custom"
LOG_MODES = (
    LOG_MODE_SIMPLE,
    LOG_MODE_DETAILED,
    LOG_MODE_COMPLETE,
    LOG_MODE_CUSTOM,
)
LOG_MODE_LABELS = {
    LOG_MODE_SIMPLE: "简易",
    LOG_MODE_DETAILED: "详细",
    LOG_MODE_COMPLETE: "完全",
    LOG_MODE_CUSTOM: "自定义",
}
LOG_MODE_LEVELS = {
    LOG_MODE_SIMPLE: 0,
    LOG_MODE_DETAILED: 1,
    LOG_MODE_COMPLETE: 2,
    LOG_MODE_CUSTOM: 2,
}

SIMPLE_LOG_CATEGORIES = (
    LOG_RUN,
    LOG_STEP,
    LOG_FLOW,
    LOG_APPLICATION,
)
DETAILED_LOG_CATEGORIES = (
    LOG_RUN,
    LOG_STEP,
    LOG_FLOW,
    LOG_ACTION,
    LOG_RECOGNITION,
    LOG_COORDINATES,
    LOG_TIMING,
    LOG_APPLICATION,
    LOG_TIMESTAMP,
)
COMPLETE_LOG_CATEGORIES = (
    LOG_RUN,
    LOG_STEP,
    LOG_FLOW,
    LOG_ACTION,
    LOG_RECOGNITION,
    LOG_COORDINATES,
    LOG_PARAMETERS,
    LOG_TIMING,
    LOG_BACKEND,
    LOG_ADAPTIVE,
    LOG_APPLICATION,
    LOG_TIMESTAMP,
)
PRESET_LOG_CATEGORIES = {
    LOG_MODE_SIMPLE: SIMPLE_LOG_CATEGORIES,
    LOG_MODE_DETAILED: DETAILED_LOG_CATEGORIES,
    LOG_MODE_COMPLETE: COMPLETE_LOG_CATEGORIES,
}
DEFAULT_CUSTOM_LOG_CATEGORIES = DETAILED_LOG_CATEGORIES


def log_mode_from_level(level) -> str:
    """Map a legacy numeric log level to the corresponding log mode string.

    Args:
        level: A legacy integer level (0=simple, 1=detailed, 2=complete).
            Non-numeric or out-of-range values default to simple.

    Returns:
        One of ``LOG_MODE_SIMPLE``, ``LOG_MODE_DETAILED``, or
        ``LOG_MODE_COMPLETE``.
    """
    try:
        numeric = int(level)
    except (TypeError, ValueError):
        numeric = 0
    return {
        0: LOG_MODE_SIMPLE,
        1: LOG_MODE_DETAILED,
        2: LOG_MODE_COMPLETE,
    }.get(numeric, LOG_MODE_SIMPLE)


def normalize_log_mode(value, legacy_level=0) -> str:
    """Normalize a raw log-mode value into a canonical mode string.

    Accepts canonical English keys, Chinese aliases, or falls back to a
    legacy numeric level when the value is unrecognized.

    Args:
        value: A mode string (English or Chinese alias) or arbitrary input.
        legacy_level: Fallback numeric level used when *value* is not
            recognized (see :func:`log_mode_from_level`).

    Returns:
        One of the canonical ``LOG_MODES`` strings.
    """
    text = str(value or "").strip().lower()
    aliases = {
        "简易": LOG_MODE_SIMPLE,
        "详细": LOG_MODE_DETAILED,
        "完全": LOG_MODE_COMPLETE,
        "自定义": LOG_MODE_CUSTOM,
    }
    if text in LOG_MODES:
        return text
    if str(value or "").strip() in aliases:
        return aliases[str(value).strip()]
    return log_mode_from_level(legacy_level)


def normalize_log_categories(value, fallback=DEFAULT_CUSTOM_LOG_CATEGORIES) -> tuple[str, ...]:
    """Normalize a raw category selection into a validated tuple of keys.

    Accepts a JSON array string, comma-separated string, or an iterable of
    category keys. Unknown keys are discarded; ordering follows the
    canonical ``LOG_CATEGORY_KEYS`` sequence.

    Args:
        value: Raw category data (str, list, tuple, set, or frozenset).
        fallback: Categories to use when *value* is not a valid collection.

    Returns:
        A tuple of validated category key strings in canonical order.
    """
    raw = value
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raw = []
        elif text.startswith("["):
            try:
                raw = loads_bounded_json(
                    text,
                    max_bytes=64 * 1024,
                    label="日志分类设置",
                    max_depth=2,
                    max_nodes=128,
                )
            except (TypeError, ValueError):
                raw = text.split(",")
        else:
            raw = text.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raw = fallback
    selected = {str(item).strip() for item in raw}
    return tuple(key for key in LOG_CATEGORY_KEYS if key in selected)


def categories_for_mode(mode, custom_categories=DEFAULT_CUSTOM_LOG_CATEGORIES) -> tuple[str, ...]:
    """Resolve the effective log-category tuple for a given mode.

    Args:
        mode: A log mode string (normalized internally).
        custom_categories: Category selection used when mode is custom.

    Returns:
        A tuple of category key strings active for the resolved mode.
    """
    normalized_mode = normalize_log_mode(mode)
    if normalized_mode == LOG_MODE_CUSTOM:
        return normalize_log_categories(custom_categories)
    return PRESET_LOG_CATEGORIES[normalized_mode]


# Emergency-stop markers are matched precisely rather than via the bare
# "急停" substring, which would also catch benign help/UI text such as
# "急停方式" or "错误、急停和严重警告". Real emergency stops emit either
# "[超时急停]" (engine timeout) or the "看门狗触发:" prefix (watchdog stops).
_CRITICAL_PATTERNS = (
    "严重崩溃", "执行线程异常", "引擎异常", "[错误]",
    "[超时急停]", "看门狗触发:", "模板无法安全识别",
    "Traceback", "FATAL", "内存不足", "std::bad_alloc", "segfault",
)


def looks_critical(message) -> bool:
    """Check whether a log message matches known critical or fatal patterns.

    Args:
        message: The log message text to inspect.

    Returns:
        True if the message contains any critical pattern (e.g. traceback,
        emergency stop marker, or fatal error indicator).
    """
    text = str(message or "")
    return any(pattern in text for pattern in _CRITICAL_PATTERNS)


@dataclass(frozen=True)
class LogPolicy:
    """Define an immutable policy governing which log categories are emitted.

    Attributes:
        mode: The canonical log mode string (simple, detailed, complete,
            or custom).
        custom_categories: Active category keys when mode is custom.
    """

    mode: str = LOG_MODE_SIMPLE
    custom_categories: tuple[str, ...] = DEFAULT_CUSTOM_LOG_CATEGORIES

    @classmethod
    def create(cls, mode, custom_categories=DEFAULT_CUSTOM_LOG_CATEGORIES) -> LogPolicy:
        """Construct a validated LogPolicy from raw mode and category inputs.

        Args:
            mode: Raw mode value (normalized via :func:`normalize_log_mode`).
            custom_categories: Raw category selection (normalized via
                :func:`normalize_log_categories`).

        Returns:
            A new :class:`LogPolicy` instance with canonical values.
        """
        return cls(
            normalize_log_mode(mode),
            normalize_log_categories(custom_categories),
        )

    @classmethod
    def from_legacy_level(cls, level) -> LogPolicy:
        """Create a LogPolicy from a legacy numeric log level.

        Args:
            level: Legacy integer level (0, 1, or 2).

        Returns:
            A new :class:`LogPolicy` with the corresponding preset mode.
        """
        return cls.create(log_mode_from_level(level))

    @property
    def enabled_categories(self) -> tuple[str, ...]:
        """Return the effective category tuple for this policy's mode."""
        return categories_for_mode(self.mode, self.custom_categories)

    @property
    def generation_level(self) -> int:
        """Return the numeric verbosity level (0-2) for the current mode."""
        return LOG_MODE_LEVELS[self.mode]

    @property
    def timestamp_enabled(self) -> bool:
        """Return True if the timestamp category is active in this policy."""
        return LOG_TIMESTAMP in self.enabled_categories

    @property
    def full_diagnostics(self) -> bool:
        """Return True if the mode enables full diagnostic-level output."""
        return self.mode in (LOG_MODE_COMPLETE, LOG_MODE_CUSTOM)

    def allows(self, category, *, critical=False, message="") -> bool:
        """Determine whether a log entry in *category* should be emitted.

        Critical messages and messages matching fatal patterns are always
        allowed regardless of the active category set.

        Args:
            category: The log category key to check.
            critical: Force-allow if True.
            message: Message text checked for critical patterns.

        Returns:
            True if the entry should be shown under this policy.
        """
        if critical or category == LOG_CRITICAL or looks_critical(message):
            return True
        normalized = str(category or LOG_ACTION)
        return normalized in self.enabled_categories

    def allows_verbose(self, category, *, critical=False, message="") -> bool:
        """Check whether verbose output is allowed for *category*.

        Requires both full-diagnostics mode and category permission.

        Args:
            category: The log category key to check.
            critical: Force-allow if True.
            message: Message text checked for critical patterns.

        Returns:
            True if verbose output for this category is permitted.
        """
        return self.full_diagnostics and self.allows(
            category, critical=critical, message=message
        )

    def enabled_labels(self) -> tuple[str, ...]:
        """Return human-readable labels for all currently enabled categories.

        Returns:
            A tuple of label strings in canonical category order.
        """
        enabled = set(self.enabled_categories)
        return tuple(
            spec.label for spec in LOG_CATEGORY_SPECS if spec.key in enabled
        )


class StructuredJsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器。

    输出每行一个 JSON 对象，包含 timestamp、level、category、message 和可选的结构化字段。
    用于诊断包导出和外部工具分析，不影响 UI 日志面板的纯文本显示。
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record into a single-line JSON string.

        Args:
            record: The log record to format.

        Returns:
            A compact JSON string with timestamp, level, category, message,
            and optional structured fields (step, action, duration, exception).
        """
        import json
        from datetime import datetime

        # DESIGN: time.strftime (used by formatTime) does not support "%f" and
        # raises ValueError on Python 3.14, so build the ISO-8601 timestamp with
        # explicit milliseconds instead of relying on a format directive.
        timestamp = datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        entry = {
            "ts": timestamp,
            "level": record.levelname,
            "category": getattr(record, "log_category", "application"),
            "message": record.getMessage(),
        }
        # 附加结构化字段（如果 record 有 extra 数据）
        if hasattr(record, "step_index"):
            entry["step"] = record.step_index
        if hasattr(record, "action_id"):
            entry["action"] = record.action_id
        if hasattr(record, "duration_ms"):
            entry["duration_ms"] = record.duration_ms
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
