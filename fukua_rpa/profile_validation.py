"""Semantic validation performed before a profile reaches Qt widgets."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .config_schema import migrate_profile_config
from .config_store import validate_profile_config_data
from .run_config import EngineRunConfig, RunConfigError
from .task_model import config_bool, parse_coordinate_text
from .win32_api import hotkey_signature, parse_hotkey_text


class ProfileValidationError(ValueError):
    """Raised when a profile fails semantic validation before reaching the UI."""


def _validated_region(value: Any, label: str) -> tuple[int, int, int, int]:
    """Validate a ``[x, y, width, height]`` region against safe bounds.

    Args:
        value: Raw region sequence from profile data.
        label: Human-readable field name used in error messages.

    Returns:
        The region as an ``(x, y, width, height)`` integer tuple.

    Raises:
        ProfileValidationError: If the region is malformed or out of range.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ProfileValidationError(f"{label}必须包含 x、y、宽度和高度")
    numbers = []
    for raw in value:
        try:
            number = float(raw)
        except (TypeError, ValueError) as error:
            raise ProfileValidationError(f"{label}包含无效坐标") from error
        if not math.isfinite(number) or not number.is_integer():
            raise ProfileValidationError(f"{label}坐标必须是有限整数")
        numbers.append(int(number))
    x, y, width, height = numbers
    if abs(x) > 10_000_000 or abs(y) > 10_000_000:
        raise ProfileValidationError(f"{label}起点超出安全范围")
    if not 1 <= width <= 1_000_000 or not 1 <= height <= 1_000_000:
        raise ProfileValidationError(f"{label}宽度和高度超出安全范围")
    return x, y, width, height


def _validate_ui_scale(value: Any) -> None:
    """Validate the interface scale stays within the supported 75%-180% band.

    Args:
        value: Raw ``ui_scale`` value, optionally with a trailing ``%``.

    Raises:
        ProfileValidationError: If the value is not a finite number in range.
    """
    text = str(value or "").strip().removesuffix("%").strip()
    try:
        scale = float(text)
    except (TypeError, ValueError) as error:
        raise ProfileValidationError("界面倍率必须是数字") from error
    if not math.isfinite(scale) or not 75 <= scale <= 180:
        raise ProfileValidationError("界面倍率必须在 75% 到 180% 之间")


def _validate_control_hotkeys(config: Mapping[str, Any]) -> None:
    """Validate the start/stop control hotkeys parse and are distinct.

    Args:
        config: Migrated profile mapping holding ``hotkey_start``/``hotkey_stop``.

    Raises:
        ProfileValidationError: If either hotkey is invalid or both match.
    """
    start = parse_hotkey_text(config.get("hotkey_start", "F9"))
    stop = parse_hotkey_text(config.get("hotkey_stop", "F10"))
    if start is None:
        raise ProfileValidationError("启动热键格式无效")
    if stop is None:
        raise ProfileValidationError("停止热键格式无效")
    if hotkey_signature(start) == hotkey_signature(stop):
        raise ProfileValidationError("启动热键和停止热键不能相同")


def _validate_key_mappings(config: Mapping[str, Any]) -> None:
    """Validate every key-mapping entry's action, hotkey, coordinate and binding.

    Args:
        config: Migrated profile mapping holding ``key_mappings``.

    Raises:
        ProfileValidationError: If any enabled mapping lacks a valid hotkey and
            coordinate, or any field fails its format check.
    """
    valid_actions = {"左键单击", "左键双击", "右键单击"}
    for index, mapping in enumerate(config.get("key_mappings", []), start=1):
        label = f"按键映射 {index}"
        action = str(mapping.get("action", "左键单击"))
        if action not in valid_actions:
            raise ProfileValidationError(f"{label}的点击动作无效")
        hotkey = str(mapping.get("hotkey", "") or "").strip()
        coordinate = str(mapping.get("coord", "") or "").strip()
        enabled = config_bool(mapping.get("enabled", False))
        if hotkey and parse_hotkey_text(hotkey) is None:
            # 报错必须带出实际值：否则用户只知道「第 N 条无效」，
            # 却不知道该改什么，且方案切换被整份拒绝后无法进入界面修正。
            raise ProfileValidationError(
                f"{label}的热键「{hotkey}」无法识别"
                "（支持 F1–F12、字母、数字与 Ctrl/Alt/Shift/Win 组合）"
            )
        if coordinate and parse_coordinate_text(coordinate) is None:
            raise ProfileValidationError(f"{label}的坐标格式无效")
        if enabled and (not hotkey or not coordinate):
            raise ProfileValidationError(
                f"{label}已启用，但没有填写有效热键和坐标"
            )
        if not isinstance(mapping.get("window_binding", {}), Mapping):
            raise ProfileValidationError(f"{label}的窗口绑定格式无效")


def validate_profile_for_ui(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached migrated profile that is safe to apply to widgets.

    Runs structural validation, schema migration, run-config assembly and a set
    of semantic checks (hotkeys, UI scale, theme, refresh interval, scan
    regions, key-mapping hotkey semantics) so that any value reaching a Qt
    control has already been bounded and normalized. 注意（FR-20260812 文档#2）：
    "按键映射一致性"（key_mapping_count 与映射列表长度的关系）不在此层校验。

    Args:
        config: Raw profile mapping loaded from storage or import.

    Returns:
        A new, migrated profile dict safe to bind to the UI.

    Raises:
        ProfileValidationError: If any structural or semantic check fails.
    """

    structural_error = validate_profile_config_data(config)
    if structural_error:
        raise ProfileValidationError(structural_error)
    prepared = migrate_profile_config(config).value
    structural_error = validate_profile_config_data(prepared)
    if structural_error:
        raise ProfileValidationError(structural_error)
    try:
        EngineRunConfig.from_mapping(prepared)
    except RunConfigError as error:
        raise ProfileValidationError(str(error)) from error

    _validate_control_hotkeys(prepared)
    _validate_ui_scale(prepared.get("ui_scale", "100"))
    if prepared.get("theme_mode", "light") not in {"light", "dark"}:
        raise ProfileValidationError("界面主题无效")

    try:
        cpu_refresh = int(prepared.get("cpu_refresh_interval", 2000))
    except (TypeError, ValueError) as error:
        raise ProfileValidationError("资源监测刷新频率无效") from error
    if cpu_refresh not in {0, 500, 1000, 2000, 3000, 5000}:
        raise ProfileValidationError("资源监测刷新频率不受支持")

    if prepared.get("run_status_pos", "右上角") not in {"右上角", "右下角"}:
        raise ProfileValidationError("运行状态提示位置无效")
    if prepared.get("mapping_click_mode", "真实鼠标点击") not in {
        "真实鼠标点击",
        "点击后返回原位",
        "后台窗口点击(实验)",
    }:
        raise ProfileValidationError("按键映射点击方式无效")

    scan_regions = prepared.get("scan_regions", [])
    if not isinstance(scan_regions, list):
        raise ProfileValidationError("多识别区域格式无效")
    if len(scan_regions) > 128:
        raise ProfileValidationError("多识别区域数量超过上限 128")
    for index, region in enumerate(scan_regions, start=1):
        _validated_region(region, f"第 {index} 个识别区域")
    scan_region = prepared.get("scan_region")
    if scan_region is not None:
        _validated_region(scan_region, "识别区域")

    _validate_key_mappings(prepared)
    return prepared
