"""Validated immutable runtime settings and task snapshots."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .commands import effective_task_settings
from .constants import (
    CLICK_SPATIAL_JITTER_DEFAULT,
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    JITTER_MAX_SECONDS,
    KEY_HOLD_DURATION_DEFAULT,
    MAX_WAIT_SECONDS,
    MOVE_CURVE_STRENGTH_DEFAULT,
    SCALE_MAX,
    SCALE_MIN,
)
from .log_policy import (
    LOG_MODE_LEVELS,
    LOG_MODES,
    normalize_log_categories,
    normalize_log_mode,
)
from .task_model import config_bool
from .vision.core import build_scale_values
from .vision.scale_memory import (
    MAX_HISTORY_LIMIT,
    MAX_LEARNED_PREFERRED,
    MIN_HISTORY_LIMIT,
    SCALE_MEMORY_TIERS,
    parse_manual_scales,
)
from .vision.scene_wake import SCENE_WAKE_SENSITIVITIES
from .workflow_document import materialize_runtime_references

if TYPE_CHECKING:
    from .engine.core import RPAEngine


class RunConfigError(ValueError):
    """Raised when user-supplied run settings fail strict validation."""
    pass


def _number(
    label: str,
    raw: Any,
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    integer: bool = False,
    strictly_positive: bool = False,
) -> float | int:
    """Parse a raw setting value into a bounded finite number.

    Args:
        label: Human-readable field name used in error messages.
        raw: Raw value from the config mapping (coerced via str()).
        minimum: Optional inclusive lower bound.
        maximum: Optional inclusive upper bound.
        integer: When True, require a whole number and return an int.
        strictly_positive: When True, reject values <= 0.

    Returns:
        The parsed value as int when *integer* is set, otherwise float.

    Raises:
        RunConfigError: If the value is non-numeric, non-finite, non-integer
            when required, or outside the supplied bounds.
    """
    text = str(raw).strip()
    try:
        value = float(text)
    except (TypeError, ValueError) as error:
        raise RunConfigError(f"设置里的“{label}”必须是数字！\n填入内容: {text}") from error
    if not math.isfinite(value):
        raise RunConfigError(f"设置里的“{label}”必须是有限数字！\n填入内容: {text}")
    if integer and not value.is_integer():
        raise RunConfigError(f"设置里的“{label}”必须是整数！\n填入内容: {text}")
    if strictly_positive and value <= 0:
        raise RunConfigError(f"设置里的“{label}”必须大于 0！\n填入内容: {text}")
    if minimum is not None and value < minimum:
        raise RunConfigError(f"设置里的“{label}”不能小于 {minimum:g}！\n填入内容: {text}")
    if maximum is not None and value > maximum:
        raise RunConfigError(f"设置里的“{label}”不能大于 {maximum:g}！\n填入内容: {text}")
    return int(value) if integer else value


# ``EngineRunConfig.from_mapping`` is split into the field-group parsers below.
# They are invoked in the same order the original monolithic body validated
# fields, so the first error raised for any invalid input is unchanged. Each
# parser returns a dict of dataclass field values.


def _parse_recognition_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate image-recognition fields: confidence, scale range and grayscale."""
    confidence = float(_number("全局相似度", config.get("conf", "0.8"), CONFIDENCE_MIN, CONFIDENCE_MAX))  # constants.CONFIDENCE_MIN/MAX
    min_scale = float(_number("最小缩放", config.get("scale_min", "0.8"), SCALE_MIN, SCALE_MAX))  # constants.SCALE_MIN/MAX
    max_scale = float(_number("最大缩放", config.get("scale_max", "1.2"), SCALE_MIN, SCALE_MAX))  # constants.SCALE_MIN/MAX
    scale_step = float(
        _number("缩放步长", config.get("scale_step", "0.05"), strictly_positive=True)
    )
    try:
        build_scale_values(min_scale, max_scale, scale_step)
    except ValueError as error:
        raise RunConfigError(f"全局缩放设置无效：{error}") from error
    return {
        "confidence": confidence,
        "min_scale": min_scale,
        "max_scale": max_scale,
        "scale_step": scale_step,
        "enable_grayscale": config_bool(config.get("gray_en", True)),
    }


def _parse_click_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate mouse-move, click-hold, jitter, timing and playback fields."""
    return {
        "move_duration": float(
            _number("移动耗时", config.get("move_spd", "0"), 0.0, 60.0)
        ),
        "move_duration_jitter": float(
            _number("移动随机延迟", config.get("move_jitter", "0"), 0.0, JITTER_MAX_SECONDS)  # constants.JITTER_MAX_SECONDS
        ),
        "click_hold": float(
            _number("按住时长", config.get("click_hld", "0.04"), 0.0, JITTER_MAX_SECONDS)  # constants.JITTER_MAX_SECONDS
        ),
        "click_hold_jitter": float(
            _number("按住随机延迟", config.get("click_hld_jitter", "0"), 0.0, JITTER_MAX_SECONDS)  # constants.JITTER_MAX_SECONDS
        ),
        "click_spatial_jitter": int(
            _number(
                "点击偏移",
                config.get("click_spatial_jitter", CLICK_SPATIAL_JITTER_DEFAULT),
                0,
                20,
                integer=True,
            )
        ),
        "move_curve_strength": float(
            _number(
                "移动曲线强度",
                config.get("move_curve_strength", MOVE_CURVE_STRENGTH_DEFAULT),
                0.0,
                1.0,
            )
        ),
        "key_hold_duration": float(
            _number(
                "系统按键按住时长",
                config.get("key_hold_duration", KEY_HOLD_DURATION_DEFAULT),
                0.0,
                3600.0,
            )
        ),
        "settlement_wait": float(
            _number("步间隔", config.get("settle", "0.5"), 0.0, MAX_WAIT_SECONDS)  # constants.MAX_WAIT_SECONDS
        ),
        "timeout_val": float(
            _number("单步超时", config.get("timeout", "0"), 0.0, MAX_WAIT_SECONDS)  # constants.MAX_WAIT_SECONDS
        ),
        "detect_delay": float(
            _number("识别间隔", config.get("detect_delay", "0.1"), 0.0, 60.0)
        ),
        "playback_speed": float(
            _number(
                "倍速执行",
                config.get("playback_speed", "1"),
                maximum=100.0,
                strictly_positive=True,
            )
        ),
        "show_click_indicator": config_bool(config.get("click_indicator", True)),
        "show_mouse_trail": config_bool(config.get("mouse_trail", False)),
        "adaptive_backoff": config_bool(config.get("adaptive_backoff", True)),
        "activate_before_click": config_bool(
            config.get("activate_before_click", False)
        ),
    }


def _parse_scroll_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate scroll/double-dodge spacing fields."""
    return {
        "double_dodge_wait": float(
            _number("二段避让间隔", config.get("dbl_wait", "0.015"), 0.0, 60.0)
        ),
    }


def _parse_auxiliary_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate auxiliary sound-alert path and playback duration fields."""
    return {
        "sound_alert_audio_path": str(
            config.get("sound_alert_audio_path", "")
        ).strip(),
        # 全局默认播放时长（秒）。0 表示不截断；缺省回落到 30。每步可在“小齿轮”
        # 中单独覆盖，且优先级高于此全局值。
        "sound_alert_play_seconds": float(
            _number(
                "声音提示播放时长",
                config.get("sound_alert_play_seconds", 30),
                0,
                600,
            )
        ),
    }


def _parse_dodge_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate dodge coordinates, toggles and post-dodge click action."""
    dodge_click_action = str(
        config.get("dodge_click_action", "none")
    ).strip().lower()
    if dodge_click_action not in ("none", "left", "right"):
        raise RunConfigError(
            f"设置里的“避让后操作”无效：{dodge_click_action}"
        )
    return {
        "dodge_x1": int(_number("避让坐标1 X", config.get("dodge_x1", "100"), integer=True)),
        "dodge_y1": int(_number("避让坐标1 Y", config.get("dodge_y1", "100"), integer=True)),
        "dodge_x2": int(_number("避让坐标2 X", config.get("dodge_x2", "200"), integer=True)),
        "dodge_y2": int(_number("避让坐标2 Y", config.get("dodge_y2", "100"), integer=True)),
        "enable_dodge": config_bool(config.get("dodge_en", False)),
        "enable_double_dodge": config_bool(config.get("dbl_dodge", False)),
        "dodge_click_action": dodge_click_action,
    }


def _parse_loop_fields(
    config: Mapping[str, Any], task_count: int | None
) -> dict[str, Any]:
    """Validate loop mode, repeat count/time limit and start/end round fields."""
    loop_mode = str(config.get("loop_mode", "单次"))
    valid_loop_modes = {
        "单次",
        "无限",
        "指定次数",
        "指定时间(时)",
        "指定时间(分)",
        "指定时间(秒)",
    }
    if loop_mode not in valid_loop_modes:
        raise RunConfigError(f"设置里的“循环模式”无效：{loop_mode}")
    time_limit_stop_mode = str(
        config.get("time_limit_stop_mode", "finish_loop")
    ).strip().lower()
    if time_limit_stop_mode not in ("finish_loop", "immediate"):
        raise RunConfigError(
            f"设置里的“限时停止方式”无效：{time_limit_stop_mode}"
        )
    loop_val = 1.0
    if loop_mode == "指定次数":
        # R-4：封顶循环次数，防止异常大值（原实现无 maximum）。
        loop_val = float(
            _number(
                "循环次数",
                config.get("loop_val", "1"),
                integer=True,
                strictly_positive=True,
                maximum=1_000_000.0,
            )
        )
    elif loop_mode in ("指定时间(时)", "指定时间(分)", "指定时间(秒)"):
        maximum_by_mode = {
            "指定时间(时)": 8_760.0,
            "指定时间(分)": 525_600.0,
            "指定时间(秒)": 31_536_000.0,
        }
        loop_val = float(
            _number(
                "运行时间",
                config.get("loop_val", "1"),
                maximum=maximum_by_mode[loop_mode],
                strictly_positive=True,
            )
        )
    else:
        try:
            loop_val = float(config.get("loop_val", 1.0))
        except (TypeError, ValueError):
            loop_val = 1.0
        if not math.isfinite(loop_val):
            loop_val = 1.0

    start_step = int(_number("从第X步开始执行", config.get("start_step", "1"), 1, integer=True))
    if task_count is not None and task_count > 0 and start_step > task_count:
        raise RunConfigError(
            f"设置里的'从第X步开始执行'必须是 1 到 {task_count} 之间的整数！\n"
            f"填入内容: {start_step}"
        )
    initial_start_raw = config.get("initial_start_step", "")
    initial_start_step_index = None
    if initial_start_raw not in (None, "", 0, "0"):
        initial_start_step = int(
            _number(
                "首轮临时起始步骤",
                initial_start_raw,
                1,
                integer=True,
            )
        )
        if (
            task_count is not None
            and task_count > 0
            and initial_start_step > task_count
        ):
            raise RunConfigError(
                f"首轮临时起始步骤必须是 1 到 {task_count} 之间的整数！\n"
                f"填入内容: {initial_start_step}"
            )
        initial_start_step_index = initial_start_step - 1
    # 最大循环轮次与 loop_val 的 1e6 兜底口径一致：防止异常大值触发
    # “等待循环范围”长期空转（审查 ENG-FR-001 的输入面收敛）。
    loop_start_round = int(
        _number(
            "脚本从第X次循环开始",
            config.get("loop_start_round", "1"),
            1,
            maximum=1_000_000.0,
            integer=True,
        )
    )
    loop_end_round = int(
        _number(
            "到第X次循环停止",
            config.get("loop_end_round", "0"),
            0,
            maximum=1_000_000.0,
            integer=True,
        )
    )
    if loop_end_round > 0 and loop_end_round < loop_start_round:
        raise RunConfigError(
            "设置里的'到第X次循环停止'不能小于起始循环！\n"
            f"起始循环: {loop_start_round}，停止循环: {loop_end_round}"
        )
    if loop_mode == "单次" and loop_start_round > 1:
        raise RunConfigError(
            "当前是'单次'循环模式，脚本起始循环必须为 1；如果要从第多次循环开始，"
            "请把循环模式改为'无限'或'指定次数'。"
        )
    return {
        "loop_mode": loop_mode,
        "loop_val": loop_val,
        "time_limit_stop_mode": time_limit_stop_mode,
        "start_step_index": start_step - 1,
        "initial_start_step_index": initial_start_step_index,
        "loop_start_round": loop_start_round,
        "loop_end_round": loop_end_round,
    }


def _parse_multi_target_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate multi-target match mode and traversal order fields."""
    multi_target_mode = str(config.get("multi_target_mode", "快速一个"))
    if multi_target_mode == "最佳一个":
        multi_target_mode = "快速一个"
    if multi_target_mode not in ("快速一个", "全部匹配"):
        raise RunConfigError(f"设置里的“多目标模式”无效：{multi_target_mode}")
    multi_target_order = str(config.get("multi_target_order", "从上到下"))
    if multi_target_order not in (
        "从上到下",
        "从左到右",
        "从右到左",
        "距离鼠标最近优先",
        "随机顺序",
    ):
        raise RunConfigError(f"设置里的“多目标顺序”无效：{multi_target_order}")
    # 第二排序条件：仅主条件严格相等时生效；默认“不启用”保持旧行为。
    multi_target_order_b = str(config.get("multi_target_order_b", "不启用"))
    if multi_target_order_b not in (
        "不启用",
        "从上到下",
        "从左到右",
        "从右到左",
        "从下到上",
    ):
        raise RunConfigError(
            f"设置里的“第二排序条件”无效：{multi_target_order_b}"
        )
    # 收集阶段（双缓冲/数量）。旧配置缺省为 0，升级后不会突然变慢。
    first_hit_buffer = _number(
        "首次命中缓冲",
        config.get("multi_collect_first_hit_s", 0),
        0,
        10,
        integer=False,
    )
    last_hit_buffer = _number(
        "末位缓冲",
        config.get("multi_collect_last_hit_s", 0),
        0,
        10,
        integer=False,
    )
    max_collect = _number(
        "最长收集时间",
        config.get("multi_collect_max_s", 0),
        0,
        600,
        integer=False,
    )
    max_targets = int(
        _number(
            "本步最多点击目标数",
            config.get("multi_collect_max_targets", 0),
            0,
            5000,
            integer=True,
        )
    )
    return {
        "multi_target_mode": multi_target_mode,
        "multi_target_order": multi_target_order,
        "multi_target_order_b": multi_target_order_b,
        "multi_collect_first_hit_s": float(first_hit_buffer),
        "multi_collect_last_hit_s": float(last_hit_buffer),
        "multi_collect_max_s": float(max_collect),
        "multi_collect_max_targets": max_targets,
        "multi_collect_allow_partial": config_bool(
            config.get("multi_collect_allow_partial", True)
        ),
    }


def _parse_input_backend_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the explicit input-injection compatibility switches."""

    mode = str(config.get("input_backend_mode", "默认"))
    if mode not in ("默认", "SendInput"):
        raise RunConfigError(f"设置里的“输入方式”无效：{mode}")
    settle = _number(
        "移动后等待",
        config.get("mouse_move_settle_s", 0),
        0,
        2,
        integer=False,
    )
    return {
        "input_backend_mode": mode,
        "keyboard_scan_code": config_bool(config.get("keyboard_scan_code", False)),
        "mouse_move_settle_s": float(settle),
    }


def _parse_log_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate log level, log mode and custom log category fields."""
    log_level = int(
        _number("日志级别", config.get("log_level", 0), 0, 2, integer=True)
    )
    raw_log_mode = config.get("log_mode")
    if raw_log_mode is not None:
        raw_log_mode_text = str(raw_log_mode).strip().lower()
        if raw_log_mode_text not in LOG_MODES and str(raw_log_mode).strip() not in (
            "简易",
            "详细",
            "完全",
            "自定义",
        ):
            raise RunConfigError(f"设置里的“日志模式”无效：{raw_log_mode}")
    log_mode = normalize_log_mode(raw_log_mode, log_level)
    log_custom_categories = normalize_log_categories(
        config.get("log_custom_categories")
    )
    log_level = LOG_MODE_LEVELS[log_mode]
    return {
        "log_level": log_level,
        "log_mode": log_mode,
        "log_custom_categories": log_custom_categories,
    }


def _parse_native_memory_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate native-core toggles and scale-memory strategy fields."""
    native_parallel_mode = str(
        config.get("native_parallel_mode", "auto")
    ).strip().lower()
    if native_parallel_mode not in ("off", "auto", "force"):
        raise RunConfigError(
            f"设置里的“原生多核模式”无效：{native_parallel_mode}"
        )
    scale_memory_tier = str(
        config.get("scale_memory_tier", "balanced")
    ).strip().lower()
    if scale_memory_tier not in SCALE_MEMORY_TIERS:
        raise RunConfigError(
            f"设置里的“缩放记忆策略”无效：{scale_memory_tier}"
        )
    try:
        scale_memory_manual = parse_manual_scales(
            config.get("scale_memory_manual", "")
        )
    except ValueError as error:
        raise RunConfigError(f"手动优先倍率无效：{error}") from error
    scale_memory_preferred_limit = int(
        _number(
            "学习优先倍率上限",
            config.get("scale_memory_preferred_limit", 3),
            1,
            MAX_LEARNED_PREFERRED,
            integer=True,
        )
    )
    scale_memory_history_limit = int(
        _number(
            "缩放历史记录上限",
            config.get("scale_memory_history_limit", 64),
            MIN_HISTORY_LIMIT,
            MAX_HISTORY_LIMIT,
            integer=True,
        )
    )
    return {
        "use_native_core": config_bool(config.get("native_core_en", True)),
        "native_parallel_mode": native_parallel_mode,
        "use_native_scale_hint": config_bool(
            config.get("native_scale_hint_en", True)
        ),
        "scale_memory_tier": scale_memory_tier,
        "scale_memory_manual": scale_memory_manual,
        "scale_memory_custom_enabled": config_bool(
            config.get("scale_memory_custom_en", False)
        ),
        "scale_memory_preferred_limit": scale_memory_preferred_limit,
        "scale_memory_history_limit": scale_memory_history_limit,
    }


def _parse_scene_wake_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate scene-wake toggle and sensitivity fields."""
    scene_wake_sensitivity = str(
        config.get("scene_wake_sensitivity", "balanced")
    ).strip().lower()
    if scene_wake_sensitivity not in SCENE_WAKE_SENSITIVITIES:
        raise RunConfigError(
            f"设置里的“画面变化灵敏度”无效：{scene_wake_sensitivity}"
        )
    return {
        "scene_wake_enabled": config_bool(config.get("scene_wake_en", True)),
        "scene_wake_sensitivity": scene_wake_sensitivity,
    }


def _parse_stop_fields(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate timeout-stop and the three emergency-stop detector toggles."""
    return {
        "timeout_stop": config_bool(config.get("timeout_stop", False)),
        "enable_tm_stop": config_bool(config.get("tm_fs", True)),
        "enable_tr_stop": config_bool(config.get("tr_fs", True)),
        "enable_key_stop": config_bool(config.get("key_fs", True)),
    }


@dataclass(frozen=True)
class EngineRunConfig:
    """Immutable, strictly validated snapshot of all engine run parameters.

    Constructed via ``from_mapping`` which rejects out-of-range or malformed
    values early, before any execution side-effects occur.
    """

    confidence: float
    min_scale: float
    max_scale: float
    scale_step: float
    enable_grayscale: bool
    use_native_core: bool
    native_parallel_mode: str
    use_native_scale_hint: bool
    scale_memory_tier: str
    scale_memory_manual: tuple[float, ...]
    scale_memory_custom_enabled: bool
    scale_memory_preferred_limit: int
    scale_memory_history_limit: int
    dodge_x1: int
    dodge_y1: int
    dodge_x2: int
    dodge_y2: int
    enable_dodge: bool
    enable_double_dodge: bool
    double_dodge_wait: float
    dodge_click_action: str
    move_duration: float
    move_duration_jitter: float
    click_hold: float
    click_hold_jitter: float
    click_spatial_jitter: int
    move_curve_strength: float
    key_hold_duration: float
    settlement_wait: float
    timeout_val: float
    timeout_stop: bool
    detect_delay: float
    adaptive_backoff: bool
    activate_before_click: bool
    scene_wake_enabled: bool
    scene_wake_sensitivity: str
    show_click_indicator: bool
    show_mouse_trail: bool
    playback_speed: float
    start_step_index: int
    initial_start_step_index: int | None
    loop_start_round: int
    loop_end_round: int
    multi_target_mode: str
    multi_target_order: str
    multi_target_order_b: str
    multi_collect_first_hit_s: float
    multi_collect_last_hit_s: float
    multi_collect_max_s: float
    multi_collect_max_targets: int
    multi_collect_allow_partial: bool
    input_backend_mode: str
    keyboard_scan_code: bool
    mouse_move_settle_s: float
    loop_mode: str
    loop_val: float
    time_limit_stop_mode: str
    log_level: int
    log_mode: str
    log_custom_categories: tuple[str, ...]
    enable_tm_stop: bool
    enable_tr_stop: bool
    enable_key_stop: bool
    sound_alert_audio_path: str
    sound_alert_play_seconds: float

    @classmethod
    def from_mapping(
        cls, config: Mapping[str, Any], task_count: int | None = None
    ) -> EngineRunConfig:
        """Parse and strictly validate a raw config mapping into an EngineRunConfig.

        Args:
            config: Raw settings dict (typically from the profile).
            task_count: Number of tasks; used to bound start_step_index.

        Raises:
            RunConfigError: If any field is missing, malformed, or out of range.
        """
        if not isinstance(config, Mapping):
            raise RunConfigError("运行配置不是有效的配置对象")
        fields: dict[str, Any] = {}
        for parser in (
            _parse_recognition_fields,
            _parse_click_fields,
            _parse_scroll_fields,
            _parse_auxiliary_fields,
            _parse_dodge_fields,
        ):
            fields.update(parser(config))
        fields.update(_parse_loop_fields(config, task_count))
        for parser in (
            _parse_multi_target_fields,
            _parse_input_backend_fields,
            _parse_log_fields,
            _parse_native_memory_fields,
            _parse_scene_wake_fields,
            _parse_stop_fields,
        ):
            fields.update(parser(config))
        return cls(**fields)

    def apply_to(self, engine: RPAEngine) -> None:
        """Push all validated settings onto a live engine instance.

        Args:
            engine: Target execution engine whose runtime attributes are
                overwritten with this config's validated values. Its log
                policy is reconfigured and fast-screenshot mode is enabled.
        """
        for field_name, engine_name in (
            ("confidence", "confidence"),
            ("min_scale", "min_scale"),
            ("max_scale", "max_scale"),
            ("scale_step", "scale_step"),
            ("enable_grayscale", "enable_grayscale"),
            ("use_native_core", "use_native_core"),
            ("native_parallel_mode", "native_parallel_mode"),
            ("use_native_scale_hint", "use_native_scale_hint"),
            ("scale_memory_tier", "scale_memory_tier"),
            ("scale_memory_manual", "scale_memory_manual"),
            ("scale_memory_custom_enabled", "scale_memory_custom_enabled"),
            ("scale_memory_preferred_limit", "scale_memory_preferred_limit"),
            ("scale_memory_history_limit", "scale_memory_history_limit"),
            ("dodge_x1", "dodge_x1"),
            ("dodge_y1", "dodge_y1"),
            ("dodge_x2", "dodge_x2"),
            ("dodge_y2", "dodge_y2"),
            ("enable_dodge", "enable_dodge"),
            ("enable_double_dodge", "enable_double_dodge"),
            ("double_dodge_wait", "double_dodge_wait"),
            ("dodge_click_action", "dodge_click_action"),
            ("move_duration", "move_duration"),
            ("move_duration_jitter", "move_duration_jitter"),
            ("click_hold", "click_hold"),
            ("click_hold_jitter", "click_hold_jitter"),
            ("click_spatial_jitter", "click_spatial_jitter"),
            ("move_curve_strength", "move_curve_strength"),
            ("key_hold_duration", "key_hold_duration"),
            ("settlement_wait", "settlement_wait"),
            ("timeout_val", "timeout_val"),
            ("timeout_stop", "timeout_stop"),
            ("detect_delay", "detect_delay"),
            ("adaptive_backoff", "adaptive_backoff"),
            ("activate_before_click", "activate_before_click"),
            ("scene_wake_enabled", "scene_wake_enabled"),
            ("scene_wake_sensitivity", "scene_wake_sensitivity"),
            ("show_click_indicator", "show_click_indicator"),
            ("show_mouse_trail", "show_mouse_trail"),
            ("playback_speed", "playback_speed"),
            ("start_step_index", "start_step_index"),
            ("initial_start_step_index", "initial_start_step_index"),
            ("loop_start_round", "loop_start_round"),
            ("loop_end_round", "loop_end_round"),
            ("multi_target_mode", "multi_target_mode"),
            ("multi_target_order", "multi_target_order"),
            ("multi_target_order_b", "multi_target_order_b"),
            ("multi_collect_first_hit_s", "multi_collect_first_hit_s"),
            ("multi_collect_last_hit_s", "multi_collect_last_hit_s"),
            ("multi_collect_max_s", "multi_collect_max_s"),
            ("multi_collect_max_targets", "multi_collect_max_targets"),
            ("multi_collect_allow_partial", "multi_collect_allow_partial"),
            ("loop_mode", "loop_mode"),
            ("loop_val", "loop_val"),
            ("time_limit_stop_mode", "time_limit_stop_mode"),
            ("enable_tm_stop", "enable_tm_stop"),
            ("enable_tr_stop", "enable_tr_stop"),
            ("enable_key_stop", "enable_key_stop"),
            ("sound_alert_audio_path", "sound_alert_audio_path"),
            ("sound_alert_play_seconds", "sound_alert_play_seconds"),
        ):
            setattr(engine, engine_name, getattr(self, field_name))
        # 输入方式与移动后等待是显式兼容开关：只有用户选择 SendInput 时才替换
        # 后端；默认仍是 PyAutoGUI，且不会在运行中静默切换。
        engine.configure_input_backend(
            self.input_backend_mode, key_scan_codes=self.keyboard_scan_code
        )
        engine.mouse_move_settle_s = self.mouse_move_settle_s
        engine.configure_log_policy(self.log_mode, self.log_custom_categories)
        engine.use_fast_screenshot = True
        # Contract marker consumed by engine.run_tasks to detect a forgotten
        # apply_to (which would otherwise silently fall back to engine defaults).
        engine._run_config_applied = True


@dataclass(frozen=True)
class RunRequest:
    """A detached runtime snapshot; later UI edits cannot mutate this run.

    INVARIANT: action-irrelevant drafts are filtered only in detached effective
    copies used for validation and execution. The editable profile remains
    unchanged. See ADR 0002/0025.
    """

    tasks: tuple[dict[str, Any], ...]
    config: EngineRunConfig
    profile_name: str
    # Design §4.3 / §5.5: provenance + plugin-dependency context for scheduled
    # runs. ``source`` discriminates manual vs scheduled runs ("manual" /
    # "scheduled"); ``schedule_id`` ties a run back to its schedule; and
    # ``plugin_dependencies`` is the set of plugin ids the run must keep
    # installed for the whole run. The execution controller acquires these
    # via PluginRegistry.acquire_for_run before launch and releases them via
    # release_after_run on finish or failed launch, so uninstall is blocked
    # for the run lifetime.
    source: str = "manual"
    schedule_id: str = ""
    plugin_dependencies: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        tasks: Sequence[Mapping[str, Any]],
        config: Mapping[str, Any],
        profile_name: str,
        *,
        source: str = "manual",
        schedule_id: str = "",
        plugin_dependencies: Sequence[str] = (),
    ) -> RunRequest:
        """Build a detached run snapshot from raw tasks and config.

        Materializes stable references, filters action-irrelevant drafts via
        effective_task_settings, and strictly validates the config. The
        resulting RunRequest is fully independent of later UI edits.

        Args:
            tasks: Raw task mappings from the profile editor.
            config: Raw settings dict (typically from the profile).
            profile_name: Display name of the source profile.
            source: Run provenance discriminator ("manual" or "scheduled").
            schedule_id: Identifier tying a scheduled run back to its schedule.
            plugin_dependencies: Plugin ids that must remain installed for the run.

        Returns:
            Fully detached, validated run snapshot.

        Raises:
            RunConfigError: If config validation fails.
        """
        detached = tuple(
            # INVARIANT: a valid target ID wins; a legacy numeric jump may only
            # establish a missing ID during reference materialization. Apply
            # capability filtering afterwards without mutating the profile.
            effective_task_settings(task)
            for task in materialize_runtime_references(tasks)
        )
        return cls(
            tasks=detached,
            config=EngineRunConfig.from_mapping(config, len(detached)),
            profile_name=str(profile_name),
            source=str(source),
            schedule_id=str(schedule_id),
            plugin_dependencies=tuple(plugin_dependencies),
        )

    def mutable_tasks(self) -> list[dict[str, Any]]:
        """Return a deep-copied mutable list of the snapshot tasks.

        Returns:
            A new list of task dicts; mutations do not affect the snapshot.
        """
        return copy.deepcopy(list(self.tasks))
