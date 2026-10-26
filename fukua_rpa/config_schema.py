"""Versioned profile defaults and forward-only configuration migrations."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from .commands import COMMAND_BY_CODE
from .constants import (
    DEFAULT_KEY_MAPPING_COUNT,
    MIN_SUPPORTED_PROFILE_SCHEMA_VERSION,
    PROFILE_SCHEMA_VERSION,
)
from .log_policy import (
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    LOG_MODE_LEVELS,
    LOG_MODE_SIMPLE,
    normalize_log_categories,  # pyright: ignore[reportUnknownVariableType]
    normalize_log_mode,  # pyright: ignore[reportUnknownVariableType]
)
from .workflow_document import normalize_workflow_tasks


class UnsupportedProfileVersion(ValueError):
    """Raised when a profile was written by a newer, unsupported application."""


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a single profile migration.

    Attributes:
        value: The migrated profile config dict.
        changed: Whether any field was added, removed, or normalized.
        source_version: Schema version of the input config.
        target_version: Schema version after migration.
    """

    value: dict[str, Any]
    changed: bool
    source_version: int
    target_version: int = PROFILE_SCHEMA_VERSION


def default_profile_config() -> dict[str, Any]:
    """Return a fresh, fully populated profile configuration."""

    return {
        "_schema_version": PROFILE_SCHEMA_VERSION,
        "conf": "0.8",
        "scale_min": "0.8",
        "scale_max": "1.2",
        "scale_step": "0.05",
        "gray_en": True,
        "native_core_en": True,
        "native_parallel_mode": "auto",
        "native_scale_hint_en": True,
        "isolated_runner_en": False,
        "scale_memory_tier": "balanced",
        "scale_memory_manual": "",
        "scale_memory_custom_en": False,
        "scale_memory_preferred_limit": 3,
        "scale_memory_history_limit": 64,
        "dodge_x1": "100",
        "dodge_y1": "100",
        "dodge_x2": "200",
        "dodge_y2": "100",
        "dodge_en": False,
        "dbl_dodge": False,
        "dbl_wait": "0.015",
        "dodge_click_action": "none",
        "move_spd": "0.0",
        "move_jitter": "0",
        "click_hld": "0.04",
        "click_hld_jitter": "0",
        "click_spatial_jitter": "0",
        "move_curve_strength": "0",
        "key_hold_duration": "0",
        "settle": "0.5",
        "timeout": "0.0",
        "timeout_stop": False,
        "detect_delay": "0.1",
        "adaptive_backoff": True,
        "activate_before_click": False,
        "scene_wake_en": True,
        "scene_wake_sensitivity": "balanced",
        "playback_speed": "1.0",
        "multi_target_mode": "快速一个",
        "multi_target_order": "从上到下",
        "multi_target_order_b": "不启用",
        "multi_collect_first_hit_s": "0",
        "multi_collect_last_hit_s": "0",
        "multi_collect_max_s": "0",
        "multi_collect_max_targets": "0",
        "multi_collect_allow_partial": True,
        "input_backend_mode": "默认",
        "keyboard_scan_code": False,
        "mouse_move_settle_s": "0",
        "hotkey_start": "F9",
        "hotkey_stop": "F10",
        "log_level": 0,
        "log_mode": LOG_MODE_SIMPLE,
        "log_custom_categories": list(DEFAULT_CUSTOM_LOG_CATEGORIES),
        "tm_fs": True,
        "tr_fs": True,
        "key_fs": True,
        "log_f": False,
        "log_ui": True,
        "mini": False,
        "top": False,
        "run_status_tip": True,
        "run_status_pos": "右上角",
        "click_indicator": True,
        "mouse_trail": False,
        "start_step": "1",
        "loop_start_round": "1",
        "loop_end_round": "0",
        "cpu_refresh_interval": 2000,
        "ui_scale": "100",
        "theme_mode": "light",
        "loop_mode": "单次",
        "loop_val": "10",
        "time_limit_stop_mode": "finish_loop",
        "scan_region": None,
        "scan_regions": [],
        "mapping_mode_enabled": False,
        "mapping_click_mode": "真实鼠标点击",
        "key_mapping_count": DEFAULT_KEY_MAPPING_COUNT,
        "key_mappings": [],
        # 编辑期行为：删除步骤后，指向它的跳转是否自动跟随到接替步骤。
        "step_adjust_follow": True,
        # 声音提示步骤可播放的本地音频文件（wav/mp3 等）；为空则使用系统蜂鸣。
        "sound_alert_audio_path": "",
        # 声音提示音频播放时长上限（秒）：音频短于该值则完整播放，长于则截断。
        # 仅全局默认值；每步可在“小齿轮”中单独覆盖（优先级更高）。0 表示不截断。
        "sound_alert_play_seconds": 30,
        "tasks": [],
    }


def _profile_version(config: Mapping[str, Any]) -> int:
    raw = config.get("_schema_version", 0)
    try:
        version = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"方案版本号无效：{raw}") from error
    if version < 0:
        raise ValueError(f"方案版本号不能小于 0：{version}")
    if version < MIN_SUPPORTED_PROFILE_SCHEMA_VERSION:
        raise UnsupportedProfileVersion(
            f"方案版本 {version} 低于本程序支持的最低版本 "
            f"{MIN_SUPPORTED_PROFILE_SCHEMA_VERSION}。请使用对应旧版导出，"
            "或使用提供转换功能的新版本处理。"
        )
    if version > PROFILE_SCHEMA_VERSION:
        raise UnsupportedProfileVersion(
            f"方案版本 {version} 高于本程序支持的版本 {PROFILE_SCHEMA_VERSION}，"
            "请使用更新版本的 fukuaRPA 打开，以免配置丢失。"
        )
    return version


def migrate_profile_config(config: Mapping[str, Any]) -> MigrationResult:
    """Migrate one profile without discarding fields unknown to this version."""

    if not isinstance(config, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("方案不是有效的配置对象")
    source_version = _profile_version(config)
    migrated = copy.deepcopy(dict(config))
    changed = source_version != PROFILE_SCHEMA_VERSION

    # Removed in v1.0.14: the old switch only duplicated the resource refresh
    # selector and made fallback hotkeys slower. Normalize its two legacy
    # values while keeping unrelated future fields untouched.
    if "low_power_ui" in migrated:
        migrated.pop("low_power_ui", None)
        changed = True
    if migrated.get("cpu_refresh_interval") in (None, "", "auto"):
        migrated["cpu_refresh_interval"] = 2000
        changed = True

    # Version 0 is every profile written before an explicit schema existed.
    if source_version == 0:
        if migrated.get("multi_target_mode") == "最佳一个":
            migrated["multi_target_mode"] = "快速一个"
        if "scan_regions" not in migrated:
            region = migrated.get("scan_region")
            migrated["scan_regions"] = [region] if region else []

    if source_version < 2:
        migrated["log_mode"] = normalize_log_mode(
            migrated.get("log_mode"), migrated.get("log_level", 0)
        )
        migrated.setdefault(
            "log_custom_categories", list(DEFAULT_CUSTOM_LOG_CATEGORIES)
        )

    defaults = default_profile_config()
    for key, value in defaults.items():
        if key not in migrated:
            migrated[key] = copy.deepcopy(value)
            changed = True

    normalized_log_mode = normalize_log_mode(
        migrated.get("log_mode"), migrated.get("log_level", 0)
    )
    normalized_log_categories = list(
        normalize_log_categories(migrated.get("log_custom_categories"))
    )
    if migrated.get("log_mode") != normalized_log_mode:
        migrated["log_mode"] = normalized_log_mode
        changed = True
    if migrated.get("log_custom_categories") != normalized_log_categories:
        migrated["log_custom_categories"] = normalized_log_categories
        changed = True
    expected_log_level = LOG_MODE_LEVELS[normalized_log_mode]
    if migrated.get("log_level") != expected_log_level:
        migrated["log_level"] = expected_log_level
        changed = True

    # The whole config (including ``tasks``) was already deep-copied and
    # detached from the source object at the top of this function, so the
    # in-place mutations below are safe; normalize_workflow_tasks deep-copies
    # again on its own.
    if isinstance(migrated.get("tasks"), list):
        for task in migrated["tasks"]:
            if not isinstance(task, dict):
                continue
            task_dict = cast(dict[str, Any], task)
            try:
                raw_type: Any = task_dict.get("type")
                command = float(raw_type)
            except (TypeError, ValueError):
                continue
            if command in COMMAND_BY_CODE and task_dict.get("type") != command:
                task_dict["type"] = command
                changed = True
        normalized_tasks = normalize_workflow_tasks(migrated["tasks"])
        if normalized_tasks != migrated["tasks"]:
            migrated["tasks"] = normalized_tasks
            changed = True
    # key_mappings and scan_regions are already detached by the top-level
    # deepcopy (line 150); no additional copy is needed here.

    if migrated.get("_schema_version") != PROFILE_SCHEMA_VERSION:
        migrated["_schema_version"] = PROFILE_SCHEMA_VERSION
        changed = True
    return MigrationResult(migrated, changed, source_version)


def migrate_profiles(profiles: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Migrate all profiles in a collection to the current schema version.

    Args:
        profiles: Mapping of profile names to raw config dicts.

    Returns:
        A tuple of (migrated profiles dict, whether any change was made).

    Raises:
        ValueError: If profiles is not a valid mapping.
    """
    if not isinstance(profiles, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("方案集合为空或格式错误")
    result: dict[str, dict[str, Any]] = {}
    changed = False
    for name, config in profiles.items():
        migration = migrate_profile_config(config)
        result[str(name)] = migration.value
        changed = changed or migration.changed or not isinstance(name, str)  # pyright: ignore[reportUnnecessaryIsInstance]
    return result, changed
