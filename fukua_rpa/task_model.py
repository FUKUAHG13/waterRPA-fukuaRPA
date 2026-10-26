"""Pure task parsing and coordinate-path helpers shared by UI and engine."""

from __future__ import annotations

import copy
import json
import math
import os
from collections.abc import Mapping
from typing import Any

from .constants import UNTIL_CONDITION_MODES
from .json_safety import loads_bounded_json

MAX_MANUAL_POINTS_JSON_BYTES = 256 * 1024
MAX_MANUAL_POINT_RECORDS = 10_000


def _finite_float_or_none(value: Any) -> float | None:
    """Return a finite float for a numeric value, otherwise ``None``."""

    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def config_bool(value: Any) -> bool:
    """Coerce a config value to bool, recognizing common truthy strings."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "启用", "是")
    return bool(value)


def task_is_disabled(task: Mapping[str, Any]) -> bool:
    """Return whether a workflow step is temporarily excluded from execution."""

    return bool(task) and config_bool(task.get("step_disabled", False))


def parse_coordinate_text(value: Any) -> tuple[int, int] | None:
    """Parse an 'x,y' string into an integer coordinate pair, or None."""
    try:
        text = str(value).strip()
        if "," not in text:
            return None
        parts = text.split(",")
        if len(parts) != 2:
            return None
        return int(parts[0].strip()), int(parts[1].strip())
    except (TypeError, ValueError):
        return None


def parse_float_text(value: Any, default: float = 0.0) -> float:
    """Parse a finite value as float, returning *default* on failure.

    ``float`` accepts ``nan`` and infinity, but neither value is a valid
    persisted setting or screen coordinate. Reject them at this shared
    boundary so callers do not each need a separate non-finite check.
    """
    try:
        parsed = float(str(value).strip())
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def parse_region_text(value: Any) -> tuple[int, int, int, int] | None:
    """Parse an 'x,y,w,h' region string, or None if invalid."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parts = [part.strip() for part in text.replace("，", ",").split(",")]
        if len(parts) != 4:
            return None
        x, y, width, height = [int(float(part)) for part in parts]
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float("inf")) —— 异常数值字符串（如 "inf"/"1e309"）
        # 需静默拒绝（ARCHITECTURE #61），不得让异常逃逸到加载/预览路径。
        return None


def format_region_text(region: Any) -> str:
    """Format a 4-element region as 'x,y,w,h' text, or '' on failure."""
    try:
        x, y, width, height = [int(float(value)) for value in region]
        return f"{x},{y},{width},{height}"
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float("inf")) —— 异常数值字符串静默返回空串。
        return ""


def until_condition_defaults() -> dict[str, Any]:
    """Return default field values for the three until-condition slots."""
    data: dict[str, Any] = {
        "until_logic": "全部满足",
        "until_false_jump": "0",
        "until_true_jump": "0",
        "until_max_checks": "0",
        "until_max_seconds": "0",
        "until_on_limit": "继续下一步",
    }
    for index in range(1, 4):
        data.update({
            f"until_cond{index}_en": index == 1,
            f"until_cond{index}_mode": "图片出现",
            f"until_cond{index}_image": "",
            f"until_cond{index}_region": "",
            # DESIGN: 子区域变化检测——仅“区域发生变化”条件使用。存储相对于 region
            # 的 x,y,w,h 像素偏移（空串表示不启用）。启用后只对该子区域做帧间比较，
            # 忽略大区域其余部分。与覆盖匹配（overlay_*，模板匹配）是不同语义。
            f"until_cond{index}_sub_region": "",
            f"until_cond{index}_conf": "0.8",
            f"until_cond{index}_diff": "8",
            f"until_cond{index}_similarity": "90",
            # DESIGN: 覆盖匹配字段——在大区域截图的指定子位置上匹配小模板图。
            # 无这些字段时行为不变（向后兼容）。参见 docs/设计基准.md 条件语义。
            f"until_cond{index}_overlay_image": "",
            f"until_cond{index}_overlay_rect": "",
            f"until_cond{index}_overlay_scale": "1.0",
            f"until_cond{index}_overlay_threshold": "0.8",
            # DESIGN: 持续时间——仅"区域不变"条件使用。>0 时要求区域连续稳定
            # 达到指定秒数才判定条件成立；0 表示差异低于阈值立即成立（向后兼容）。
            f"until_cond{index}_stable_seconds": "0",
        })
    return data


# INVARIANT: single source of truth for the default value of every workflow step
# ``custom_data`` field. Both the editable row constructor and the data
# binding loader derive their defaults from this mapping so the two cannot
# drift apart. Mutable containers are deep-copied at the point of use.
TASK_DATA_DEFAULTS: dict[str, Any] = {
    # --- 基础 / 编辑器 ---
    "step_id": "",
    "step_disabled": False,
    "step_note": "",
    "marker_color": "",
    # DESIGN: editor_hidden and editor_groups organize the editor only. They do
    # not disable execution; step_disabled is the sole persisted disable flag.
    "editor_hidden": False,
    "editor_groups": [],
    # --- 图片识别 ---
    "custom_en": False,
    "custom_conf": "0.8",
    "custom_scale_min": "1.0",
    "custom_scale_max": "1.0",
    "custom_scale_step": "0.05",
    "custom_gray": True,
    "inverse_match_en": False,
    "image_confirm_click_en": False,
    "image_confirm_click_delay": "0.5",
    "template_mask_regions": [],
    "delayed_second_click_en": False,
    "delayed_second_click_delay": "0.5",
    # --- 多图动作 ---
    "multi_image_actions": [],
    # --- 循环与跳转 ---
    "repeat_mode": "执行一次",
    "repeat_count": "1",
    "step_loop_start": "1",
    "step_loop_end": "0",
    "fail_limit": "1",
    "success_skip": "0",
    "success_jump": "0",
    "success_target_id": "",
    "fail_skip": "0",
    "fail_jump": "0",
    "fail_target_id": "",
    "call_step": "0",
    "call_target_id": "",
    "jump_only": False,
    "no_skip_wait": False,
    # --- 坐标与区域 ---
    "point_limit_en": False,
    "point_limit_count": "0",
    "image_click_point_en": False,
    "image_click_point_rx": "0.5",
    "image_click_point_ry": "0.5",
    "step_region_en": False,
    "step_region": "",
    "capture_region_mode": "global",
    "capture_region": "",
    # --- 坐标步进 ---
    "coord_step_en": False,
    "coord_step_every": "1",
    "coord_step_direction": "向下",
    "coord_step_distance": "0",
    "coord_step_dx": "0",
    "coord_step_dy": "0",
    "coord_step_point": "",
    "coord_step_max_steps": "0",
    "coord_step_max_distance": "0",
    "coord_step_stop": False,
    "coord_step_reset_after": "0",
    "coord_step_manual_points": "{}",
    # --- 自定义点位序列 ---
    "coord_sequence_en": False,
    "coord_sequence_points": "",
    "coord_sequence_end_action": "点完后跳过本步",
    "coord_sequence_batch_mode": "one",
    "coord_sequence_batch_count": "1",
    # --- 后台点击 ---
    "background_click_en": False,
    "background_click_binding": {},
    # --- 运行控制与调试 ---
    "run_max_executions": "0",
    "debug_breakpoint": False,
    "debug_condition": "",
    "recorded_duration": "0",
    # --- 输入时序（类型 29）：相对时间事件流，见 recording_model.recorded_events_to_timeline ---
    "input_events": [],
    # --- 拖拽终点图片 ---
    "drag_target_img": "",
    "drag_target_point": "",
    # --- 拖拽起点图片 ---
    "drag_source_img": "",
    "drag_source_point": "",
    # --- UIA 绑定 ---
    "uia_binding": {},
    # --- 直到条件 ---
    **until_condition_defaults(),
    "until_false_target_id": "",
    "until_true_target_id": "",
}


def task_data_defaults() -> dict[str, Any]:
    """Return a fresh deep copy of ``TASK_DATA_DEFAULTS``.

    Mutable containers (lists/dicts) must not be shared across rows, so
    callers receive an independent copy rather than the shared constant.
    """

    return copy.deepcopy(TASK_DATA_DEFAULTS)


def until_condition_list_from_data(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract enabled until-condition entries from flat task data."""
    conditions: list[dict[str, Any]] = []
    for index in range(1, 4):
        if not config_bool(data.get(f"until_cond{index}_en", index == 1)):
            continue
        mode = str(data.get(f"until_cond{index}_mode", "图片出现"))
        if mode not in UNTIL_CONDITION_MODES:
            mode = "图片出现"
        conditions.append({
            "index": index,
            "mode": mode,
            "image": str(data.get(f"until_cond{index}_image", "")).strip(),
            "region": str(data.get(f"until_cond{index}_region", "")).strip(),
            "sub_region": str(data.get(f"until_cond{index}_sub_region", "")).strip(),
            "conf": str(data.get(f"until_cond{index}_conf", "0.8")).strip(),
            "diff": str(data.get(f"until_cond{index}_diff", "8")).strip(),
            "similarity": str(data.get(f"until_cond{index}_similarity", "90")).strip(),
            "until_overlay_image": str(data.get(f"until_cond{index}_overlay_image", "")).strip(),
            "until_overlay_rect": str(data.get(f"until_cond{index}_overlay_rect", "")).strip(),
            "until_overlay_scale": str(data.get(f"until_cond{index}_overlay_scale", "1.0")).strip(),
            "until_overlay_threshold": str(data.get(f"until_cond{index}_overlay_threshold", "0.8")).strip(),
            "stable_seconds": str(data.get(f"until_cond{index}_stable_seconds", "0")).strip(),
        })
    return conditions


def until_condition_summary(data: Mapping[str, Any]) -> str:
    """Build a human-readable one-line summary of until-conditions."""
    conditions = until_condition_list_from_data(data)
    if not conditions:
        return "未设置条件"
    parts: list[str] = []
    for condition in conditions[:3]:
        mode = condition["mode"]
        image = os.path.basename(condition.get("image", "")) if condition.get("image") else ""
        region = condition.get("region", "")
        if mode == "区域发生变化":
            description = f"区域变化 {region or '未选区域'}"
            sub_region = condition.get("sub_region", "")
            if sub_region:
                description += f"（子区域 {sub_region}）"
        elif mode == "区域变成指定图片":
            description = f"区域变成 {image or '未选图片'}"
        elif mode == "区域不变":
            description = f"区域不变 {region or '未选区域'}"
        elif mode == "覆盖匹配":
            overlay_img = os.path.basename(condition.get("until_overlay_image", "")) if condition.get("until_overlay_image") else ""
            description = f"覆盖匹配 {overlay_img or '未选小图'}@{region or '未选区域'}"
        else:
            description = f"{mode} {image or '未选图片'}"
            if region:
                description += f"@{region}"
        parts.append(description)
    logic = str(data.get("until_logic", "全部满足"))
    false_jump = str(data.get("until_false_jump", "0")).strip() or "0"
    true_jump = str(data.get("until_true_jump", "0")).strip() or "0"
    true_text = "下一步" if true_jump == "0" else f"第{true_jump}步"
    if len(parts) == 1:
        return f"{parts[0]}；未满足→第{false_jump}步，满足→{true_text}"
    return f"{logic}：{'；'.join(parts)}；未满足→第{false_jump}步，满足→{true_text}"


def parse_coord_step_manual_points(value: Any) -> dict[int, tuple[float, float]]:
    """Parse manual correction points from JSON into an index-to-coordinate map."""
    try:
        raw = loads_bounded_json(
            value if isinstance(value, dict) else str(value or "").strip(),
            max_bytes=MAX_MANUAL_POINTS_JSON_BYTES,
            label="手动修正点",
            max_depth=4,
            max_nodes=MAX_MANUAL_POINT_RECORDS * 4 + 8,
        )
        points: dict[int, tuple[float, float]] = {}
        for key, coordinate in raw.items():  # type: ignore[union-attr]
            if len(points) >= MAX_MANUAL_POINT_RECORDS:
                break
            index = int(key)
            if index <= 0:
                continue
            if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:  # pyright: ignore[reportUnknownArgumentType]
                x = _finite_float_or_none(coordinate[0])  # pyright: ignore[reportUnknownArgumentType]
                y = _finite_float_or_none(coordinate[1])  # pyright: ignore[reportUnknownArgumentType]
                if x is not None and y is not None:
                    points[index] = (x, y)
        return points
    except (AttributeError, OverflowError, TypeError, ValueError):
        return {}


def serialize_coord_step_manual_points(points: Mapping[int, tuple[float, float]] | None) -> str:
    """Serialize manual correction points to a compact JSON string."""
    normalized: dict[str, list[int]] = {}
    for index, coordinate in (points or {}).items():
        try:
            index = int(index)
            if index <= 0:
                continue
            x, y = coordinate
            x_value = _finite_float_or_none(x)
            y_value = _finite_float_or_none(y)
            if x_value is None or y_value is None:
                continue
            normalized[str(index)] = [int(round(x_value)), int(round(y_value))]
        except (OverflowError, TypeError, ValueError):
            continue
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def parse_coordinate_sequence(value: Any) -> list[tuple[int, int]]:
    """Parse a semicolon-separated coordinate sequence into a list of (x, y) pairs."""
    points: list[tuple[int, int]] = []
    text = str(value or "").strip()
    if not text:
        return points
    normalized = text.replace("\n", ";").replace("，", ",")
    for chunk in normalized.split(";"):
        coordinate = parse_coordinate_text(chunk.strip())
        if coordinate:
            points.append(coordinate)
    return points


def serialize_coordinate_sequence(points: list[tuple[int, int]] | None) -> str:
    """Serialize a coordinate sequence to a semicolon-separated string."""
    result: list[str] = []
    for point in points or []:
        try:
            x, y = point
            result.append(f"{int(float(x))},{int(float(y))}")
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(float("inf")) —— 异常数值跳过该点（ARCHITECTURE #61）。
            continue
    return "; ".join(result)


# DEC-20260930-002-04（维护者裁定 2026-10-07）：**不承诺乱码方向的长期兼容**。
# 仅保留下表这组**已知且可验证的固定别名**作为迁移入口。
#
# 此前这里用 direction.startswith(...) 做前缀匹配：任何以该字开头的未知值都会被
# 当成合法方向。那正是裁定所禁止的「按前缀猜测」，已移除。未知值一律原样保留，
# 并由运行前校验（validation._validate_coordinate_step）拒绝运行，绝不静默改成
# 「向上」或任何其他方向。
_DIRECTION_ALIASES = {
    "\u9359\u52dc\u7c82": "\u5411\u4e0a",
    "\u9359\u52dc\u7c85": "\u5411\u4e0b",
    "\u9359\u52dd\u4e99": "\u5411\u5de6",
    "\u9359\u52dd\u5f78": "\u5411\u53f3",
    "\u9479\ue4d1\u6d3c\u5939\u4e15\u504f": "\u81ea\u5b9a\u4e49\u504f\u79fb",
    "\u7ee2\u6d69\u52a8\u9348\u7247\u65b0\u9ede\u7d44\u4f4d": "\u79fb\u52a8\u5230\u65b0\u70b9\u4f4d",
}

#: Standard direction names; the pre-run validation accepts exactly these.
COORD_STEP_DIRECTIONS = (
    "\u5411\u4e0a",
    "\u5411\u4e0b",
    "\u5411\u5de6",
    "\u5411\u53f3",
    "\u81ea\u5b9a\u4e49\u504f\u79fb",
    "\u79fb\u52a8\u5230\u65b0\u70b9\u4f4d",
)


def normalize_coord_step_direction(direction: str) -> str:
    """Map a verified mojibake alias onto its standard direction name.

    DEC-20260930-002-04: unknown values are returned **unchanged** so the pre-run
    validation rejects them with the user's own text still visible. Nothing is
    guessed by prefix and nothing is silently rewritten.
    """
    return _DIRECTION_ALIASES.get(str(direction), str(direction))


def is_known_coord_step_direction(direction: str) -> bool:
    """Whether *direction* is a standard name or a verified alias for one."""
    return normalize_coord_step_direction(direction) in COORD_STEP_DIRECTIONS


def coord_step_delta_values(direction: str, distance: float, dx: float, dy: float) -> tuple[float, float]:
    """Return the (dx, dy) offset for a given step direction name."""
    direction = normalize_coord_step_direction(direction)
    if direction in ("\u5411\u4e0a", "鍚戜笂"):
        return 0.0, -distance
    if direction in ("\u5411\u4e0b", "鍚戜笅"):
        return 0.0, distance
    if direction in ("\u5411\u5de6", "鍚戝乏"):
        return -distance, 0.0
    if direction in ("\u5411\u53f3", "鍚戝彸"):
        return distance, 0.0
    direction = normalize_coord_step_direction(direction)
    if direction == "\u81ea\u5b9a\u4e49\u504f\u79fb":
        return dx, dy
    return 0.0, 0.0


def build_coord_step_positions(base_x: float, base_y: float, options: Mapping[str, Any], max_points: int = 200) -> list[tuple[float, float]]:
    """Compute the ordered list of click positions for coordinate-step mode."""
    base_x = _finite_float_or_none(base_x)
    base_y = _finite_float_or_none(base_y)
    if base_x is None or base_y is None:
        return []
    direction = str(options.get("direction", "向下"))
    raw_max_steps = parse_float_text(options.get("max_steps", 0), 0.0)
    max_steps = max(0, int(raw_max_steps))
    max_distance = max(0.0, parse_float_text(options.get("max_distance", 0), 0.0))
    positions = [(base_x, base_y)]

    direction = normalize_coord_step_direction(direction)
    if direction == "\u79fb\u52a8\u5230\u65b0\u70b9\u4f4d":
        # SAFETY: 旧存档可能以 mojibake 形式保存「移动到新点位」方向，保留兼容（见上方说明）。
        point = parse_coordinate_text(options.get("point", ""))
        if not point:
            return positions
        target_x = _finite_float_or_none(point[0])
        target_y = _finite_float_or_none(point[1])
        if target_x is None or target_y is None:
            return positions
        total_points = max_steps if max_steps >= 2 else 2
        for index in range(1, min(total_points, max_points)):
            ratio = index / (total_points - 1)
            x = base_x + (target_x - base_x) * ratio
            y = base_y + (target_y - base_y) * ratio
            if not math.isfinite(x) or not math.isfinite(y):
                break
            positions.append((x, y))
        manual_points = parse_coord_step_manual_points(
            options.get("manual_points", options.get("coord_step_manual_points", {}))
        )
        for index, manual_point in manual_points.items():
            if 0 < index < len(positions):
                positions[index] = manual_point
        return positions

    distance = parse_float_text(options.get("distance", 0), 0.0)
    dx = parse_float_text(options.get("dx", 0), 0.0)
    dy = parse_float_text(options.get("dy", 0), 0.0)
    step_dx, step_dy = coord_step_delta_values(direction, distance, dx, dy)
    if step_dx == 0 and step_dy == 0:
        return positions

    move_count = max_steps if max_steps > 0 else min(10, max_points - 1)
    for index in range(1, min(move_count + 1, max_points)):
        x = base_x + step_dx * index
        y = base_y + step_dy * index
        if not math.isfinite(x) or not math.isfinite(y):
            break
        distance_from_base = math.hypot(x - base_x, y - base_y)
        if max_distance > 0 and distance_from_base > max_distance:
            break
        positions.append((x, y))
    return positions
