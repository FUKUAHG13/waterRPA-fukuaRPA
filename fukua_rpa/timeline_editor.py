"""内部事件编辑的纯模型：动作分组、首版编辑操作与编辑后时序校验。

本模块不依赖 Qt：接收 ``input_events``（已保存的 29.0 事件字典列表），
返回新列表或带原因的拒绝结果。所有操作都在调用方提供的列表副本上进行，
写回、撤销与日志由控制器/对话框负责。

INVARIANT: 事件列表的原始下标是动作分组与编辑定位的唯一身份。同一时间戳
的事件按列表顺序派发，因此编辑只改允许改的字段，绝不重排事件。

首版支持：修改单击位置、调整等待、删除完整动作。拖拽、滚轮、片段变速、
替换键与逐点轨迹只做查看与原因提示，不在此提供编辑操作。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from fukua_rpa.constants import (
    MAX_TIMELINE_COORDINATE,
    MAX_TIMELINE_EVENT_SECONDS,
    MAX_TIMELINE_EVENTS,
    MOUSE_BUTTON_NAMES,
)

_KEY_ACTIONS = frozenset({"down", "up", "repeat"})
_MOUSE_ACTIONS = frozenset({"down", "up"})


@dataclass(frozen=True)
class EditResult:
    """一次编辑操作的结果：成功携带新事件列表，失败携带可读原因。"""

    ok: bool
    events: list[dict[str, Any]] | None = None
    reason: str = ""


@dataclass
class ActionGroup:
    """一个可展示/可编辑的动作分组，indices 指向原始事件下标。"""

    id: str
    kind: str  # click / drag / key / wheel / move / unpaired
    label: str
    indices: tuple[int, ...]
    t_start: float
    t_end: float
    x: int | None = None
    y: int | None = None
    detail: str = ""
    # DESIGN: 定位点关联必须有证据（同刻 + 紧邻 + 坐标一致三者同时成立）。
    # 仅同刻相邻只是线索，存入 positional_hint 供预览提示，不参与单击位置写回。
    positioning_indices: tuple[int, ...] = ()
    positional_hint: str = ""
    reason: str = ""  # 非空表示无法可靠编辑，仅可预览
    editable_click: bool = False
    editable_delete: bool = False


def _valid_point(item: dict[str, Any]) -> bool:
    try:
        x = int(item.get("x"))
        y = int(item.get("y"))
    except (TypeError, ValueError):
        return False
    return abs(x) <= MAX_TIMELINE_COORDINATE and abs(y) <= MAX_TIMELINE_COORDINATE


def _positioning_for_up(events: list[dict[str, Any]], up_index: int):
    """判定鼠标松开前的 move 是否是可确认的定位点。

    返回 (确认下标元组, 线索说明)。证据链：紧邻前一个事件是 move，且时间戳
    与松开刻相同，且坐标等于松开落点。缺任一条即降级为线索。
    """
    if up_index <= 0:
        return (), ""
    prev = events[up_index - 1]
    if prev.get("kind") != "move":
        return (), ""
    same_time = math.isclose(
        float(prev.get("t", -1)), float(events[up_index].get("t", -2)), abs_tol=1e-9
    )
    same_point = (
        prev.get("x") == events[up_index].get("x")
        and prev.get("y") == events[up_index].get("y")
    )
    if same_time and same_point:
        return (up_index - 1,), ""
    if same_time and not same_point:
        return (), "同刻存在 move，但坐标与落点不一致，定位关系不明确"
    return (), ""


def _nested_mouse_downs(events: list[dict[str, Any]]) -> set[int]:
    """标出「其它鼠标按钮仍按住时按下」的下标。

    SAFETY: 内层点击的坐标不可编辑。回放派发 mouse down 前会先 move_to(x, y)，
    若外层按钮仍按住，这次定位会在按住期间移动光标，凭空生成原始录制中不存在
    的拖拽；up 不定位，删掉内层点击也会改变外层按住期间的坐标语义。故内层点击
    与其他按钮重叠的动作一律只作预览。
    """
    nested: set[int] = set()
    held: dict[str, int] = {}
    for index, item in enumerate(events):
        if item.get("kind") != "mouse":
            continue
        button = str(item.get("button", ""))
        action = item.get("action")
        if action == "down":
            if any(count > 0 for other, count in held.items() if other != button):
                nested.add(index)
            held[button] = held.get(button, 0) + 1
        elif action == "up":
            held[button] = max(0, held.get(button, 0) - 1)
    return nested


def group_events(events: list[dict[str, Any]]) -> list[ActionGroup]:
    """把事件列表按动作语义分组，供界面展示与编辑定位。"""
    groups: list[ActionGroup] = []
    nested_downs = _nested_mouse_downs(events)
    # 已被动作分组配对消费的松开下标：跳过它们，避免每个完整动作后面多出一条
    # 「孤立的…松开」幻影行（真正的孤立松开不在集合内，仍会展示）。
    consumed: set[int] = set()
    i = 0
    while i < len(events):
        event = events[i]
        kind = event.get("kind")
        if kind == "key":
            action = event.get("action")
            key = str(event.get("key", ""))
            if action == "down":
                indices = [i]
                repeats = 0
                j = i + 1
                up_at = None
                while j < len(events):
                    nxt = events[j]
                    if (
                        nxt.get("kind") == "key"
                        and str(nxt.get("key", "")) == key
                    ):
                        if nxt.get("action") == "repeat":
                            repeats += 1
                            indices.append(j)
                        elif nxt.get("action") == "up":
                            up_at = j
                            indices.append(j)
                            break
                        elif nxt.get("action") == "down":
                            break  # 重复按下，交校验器判错
                    j += 1
                complete = up_at is not None
                groups.append(
                    ActionGroup(
                        id=f"key:{i}",
                        kind="key",
                        label=f"按键 {key}" + (f"（重复 {repeats}）" if repeats else ""),
                        indices=tuple(indices),
                        t_start=float(event.get("t", 0.0)),
                        t_end=float(events[indices[-1]].get("t", 0.0)),
                        detail=f"{key} · {action}" + (f"…{len(indices)} 事件" if len(indices) > 1 else ""),
                        reason="" if complete else "按键未松开，无法作为完整动作删除",
                        editable_delete=complete,
                    )
                )
                # 组内已消费的事件（自动重复与松开）不再单独成行：repeat 属同
                # 一按键动作并计入「（重复 N）」，单列会显示成幻影孤立行。没有
                # 对应 down 的真实孤立 repeat 不在任何组内，仍会照常显示。
                consumed.update(idx for idx in indices if idx != i)
                i += 1
                continue
            # 孤立的 repeat / up
            if i in consumed:
                i += 1
                continue
            reason = (
                "自动重复缺少对应按下" if action == "repeat" else "孤立的按键松开"
            )
            groups.append(
                ActionGroup(
                    id=f"key-orphan:{i}",
                    kind="unpaired",
                    label=f"按键 {key}（{action}）",
                    indices=(i,),
                    t_start=float(event.get("t", 0.0)),
                    t_end=float(event.get("t", 0.0)),
                    detail=key,
                    reason=reason,
                )
            )
            i += 1
            continue

        if kind == "mouse":
            action = event.get("action")
            button = str(event.get("button", ""))
            if action == "up":
                if i in consumed:
                    i += 1
                    continue
                groups.append(
                    ActionGroup(
                        id=f"mouse-orphan:{i}",
                        kind="unpaired",
                        label=f"鼠标 {button} 松开",
                        indices=(i,),
                        t_start=float(event.get("t", 0.0)),
                        t_end=float(event.get("t", 0.0)),
                        reason="孤立的鼠标松开",
                    )
                )
                i += 1
                continue
            # down：按具体按钮配对
            indices = [i]
            j = i + 1
            up_at = None
            hold_moves: list[int] = []
            multi_button = False
            while j < len(events):
                nxt = events[j]
                if nxt.get("kind") == "mouse" and nxt.get("action") == "down":
                    if str(nxt.get("button", "")) != button:
                        multi_button = True
                    j += 1
                    continue
                if (
                    nxt.get("kind") == "mouse"
                    and nxt.get("action") == "up"
                    and str(nxt.get("button", "")) == button
                ):
                    up_at = j
                    indices.append(j)
                    break
                if nxt.get("kind") == "move" and _valid_point(nxt):
                    hold_moves.append(j)
                j += 1
            complete = up_at is not None
            if complete:
                consumed.add(up_at)
            # DESIGN: 拖拽要求「按住期间」确有位移。与松开同刻的 move 是定位点
            # 候选（同刻相邻只是线索），不计入按住期间位移；否则坐标不一致的
            # 定位 move 会把一个单击误判成拖拽，用户既不能编辑也看不到原因。
            up_moment = float(events[up_at].get("t", 0.0)) if complete else 0.0
            moved = any(
                float(events[mi].get("t", 0.0)) < up_moment - 1e-9
                and (events[mi].get("x"), events[mi].get("y"))
                != (event.get("x"), event.get("y"))
                for mi in hold_moves
            )
            # SAFETY: 按下与松开落点不同，说明按住期间光标确实移动过——即使
            # 位移只体现在与松开同刻的最后一个点上（更早的 move 可能已被采样
            # 压缩掉）。这类动作是拖拽，必须排除在可删除单击之外，否则会被当作
            # 单击删除，违反「拖拽仅预览」的首版约定。
            if (
                not moved
                and complete
                and _valid_point(event)
                and _valid_point(events[up_at])
                and (event.get("x"), event.get("y"))
                != (events[up_at].get("x"), events[up_at].get("y"))
            ):
                moved = True
            positioning: tuple[int, ...] = ()
            hint = ""
            if complete:
                positioning, hint = _positioning_for_up(events, up_at)
            if moved:
                label = f"拖拽 {button}"
                gkind = "drag"
                reason = "拖拽编辑留待后续增量"
            elif multi_button:
                label = f"多键点击 {button}"
                gkind = "click"
                reason = "多按钮同时按下，无法确认独立单击"
            elif i in nested_downs:
                # SAFETY: 内层点击夹在别的按钮按住区间内，改点会在按住期间
                # 移动光标（生成原录制没有的拖拽），删除也会改变按下/松开的
                # 嵌套关系；只作预览。
                label = f"单击 {button}"
                gkind = "click"
                reason = "与其它鼠标按钮的按住区间重叠，无法确认独立单击"
            else:
                label = f"单击 {button}"
                gkind = "click"
                reason = hint if hint else ""
            groups.append(
                ActionGroup(
                    id=f"mouse:{i}",
                    kind=gkind,
                    label=label,
                    indices=tuple(indices),
                    t_start=float(event.get("t", 0.0)),
                    t_end=float(events[indices[-1]].get("t", 0.0)),
                    x=int(event.get("x", 0)) if _valid_point(event) else None,
                    y=int(event.get("y", 0)) if _valid_point(event) else None,
                    detail=f"({event.get('x')}, {event.get('y')})",
                    positioning_indices=positioning,
                    positional_hint=hint,
                    reason=reason,
                    editable_click=(
                        gkind == "click"
                        and not reason
                        and complete
                        and event.get("x") == events[up_at].get("x")
                        and event.get("y") == events[up_at].get("y")
                    )
                    if complete
                    else False,
                    # 首版只删除完整识别的单击/按键。拖拽、多键、定位关系不明
                    # 的动作是“仅预览”动作（reason 非空），不得进入删除入口，
                    # 否则只删 down/up 会留下中间 move 形成半截动作。
                    editable_delete=(
                        gkind == "click" and not reason and complete and not moved
                    ),
                )
            )
            i += 1
            continue

        if kind == "move":
            groups.append(
                ActionGroup(
                    id=f"move:{i}",
                    kind="move",
                    label="移动点",
                    indices=(i,),
                    t_start=float(event.get("t", 0.0)),
                    t_end=float(event.get("t", 0.0)),
                    x=int(event.get("x", 0)) if _valid_point(event) else None,
                    y=int(event.get("y", 0)) if _valid_point(event) else None,
                    detail=f"({event.get('x')}, {event.get('y')})",
                    reason="逐点轨迹编辑留待后续增量",
                )
            )
            i += 1
            continue

        if kind == "wheel":
            groups.append(
                ActionGroup(
                    id=f"wheel:{i}",
                    kind="wheel",
                    label="滚轮",
                    indices=(i,),
                    t_start=float(event.get("t", 0.0)),
                    t_end=float(event.get("t", 0.0)),
                    detail=f"delta={event.get('delta')}",
                    reason="滚轮未保存落点，无法关联定位点；编辑留待后续增量",
                )
            )
            i += 1
            continue

        i += 1
    return groups


@dataclass(frozen=True)
class WaitGap:
    """一段可调整的空等，boundary 是其后第一个事件的下标。"""

    boundary: int
    start: float
    end: float
    duration: float
    editable: bool
    reason: str = ""


def find_wait_gaps(events: list[dict[str, Any]]) -> list[WaitGap]:
    """列出开头空等与所有事件间隔，并标注哪些间隔期间没有按住输入。"""
    gaps: list[WaitGap] = []
    pressed_keys: set[str] = set()
    pressed_buttons: set[str] = set()
    previous = 0.0
    for index, event in enumerate(events):
        moment = float(event.get("t", 0.0))
        held = bool(pressed_keys or pressed_buttons)
        gaps.append(
            WaitGap(
                boundary=index,
                start=previous,
                end=moment,
                duration=max(0.0, moment - previous),
                editable=not held,
                reason="该间隔期间有键或按钮按住" if held else "",
            )
        )
        if event.get("kind") == "key":
            if event.get("action") == "down":
                pressed_keys.add(str(event.get("key", "")))
            elif event.get("action") == "up":
                pressed_keys.discard(str(event.get("key", "")))
        elif event.get("kind") == "mouse":
            if event.get("action") == "down":
                pressed_buttons.add(str(event.get("button", "")))
            elif event.get("action") == "up":
                pressed_buttons.discard(str(event.get("button", "")))
        previous = moment
    return gaps


def _cursor_anchor_indices(events: list[dict[str, Any]]) -> list[int | None]:
    """返回每个事件下标在派发前“最近一次设置光标位置”的事件下标。

    与回放一致：``move`` 与鼠标 ``down`` 会 ``move_to`` 定位；鼠标 ``up``、
    按键与滚轮不定位。滚轮因此总是落在“上一个定位事件”留下的光标处。
    """
    anchors: list[int | None] = []
    last: int | None = None
    for index, event in enumerate(events):
        anchors.append(last)
        if event.get("kind") == "move" or (
            event.get("kind") == "mouse" and event.get("action") == "down"
        ):
            last = index
    return anchors


def _wheel_landing_fixes(
    events: list[dict[str, Any]], changed: set[int]
) -> dict[int, tuple[int, int]] | None:
    """找出落点依赖被改/被删定位事件的滚轮，给出其原落点补偿。

    返回 ``{滚轮下标: (原x, 原y)}``；若某滚轮的定位锚点坐标无法取得则返回
    ``None``，表示无法安全保留，调用方须拒绝应用。
    """
    anchors = _cursor_anchor_indices(events)
    fixes: dict[int, tuple[int, int]] = {}
    for index, event in enumerate(events):
        if event.get("kind") != "wheel":
            continue
        anchor = anchors[index]
        if anchor is None or anchor not in changed:
            continue
        source = events[anchor]
        if not _valid_point(source):
            return None
        fixes[index] = (int(source["x"]), int(source["y"]))
    return fixes


def edit_click_position(
    events: list[dict[str, Any]],
    group_id: str,
    new_x: int,
    new_y: int,
) -> EditResult:
    """修改一次单击的落点，同步按下、松开与确认关联的定位点。"""
    try:
        x, y = int(new_x), int(new_y)
    except (TypeError, ValueError):
        return EditResult(False, reason="坐标必须是整数")
    if abs(x) > MAX_TIMELINE_COORDINATE or abs(y) > MAX_TIMELINE_COORDINATE:
        return EditResult(False, reason="坐标超出允许范围")
    group = next((g for g in group_events(events) if g.id == group_id), None)
    if group is None:
        return EditResult(False, reason="找不到该动作")
    if not group.editable_click:
        return EditResult(
            False,
            reason=group.reason or "仅支持起落点一致、按住期间无拖拽、定位关系明确的单击",
        )
    updated = [dict(event) for event in events]
    changed = set(group.indices) | set(group.positioning_indices)
    fixes = _wheel_landing_fixes(events, changed)
    if fixes is None:
        return EditResult(False, reason="后续滚轮的原落点无法确定，未应用")
    # SAFETY: 滚轮若夹在单击按住期间（down 与 up 之间），改落点会让光标在按住
    # 中途移动，把单击变成拖拽；无法同时保留单击与滚轮落点，直接拒绝。
    down_index = min(group.indices)
    up_index = max(group.indices)
    if any(down_index < wheel_index < up_index for wheel_index in fixes):
        return EditResult(
            False, reason="滚轮位于单击按住期间，无法同时保留单击与滚轮落点，未应用"
        )
    for index in changed:
        updated[index]["x"] = x
        updated[index]["y"] = y
    # 在被影响的滚轮前插入同刻 move，恢复原落点（同刻顺序：定位先于滚轮）。
    result: list[dict[str, Any]] = []
    for index, event in enumerate(updated):
        if index in fixes:
            fix_x, fix_y = fixes[index]
            result.append(
                {"t": round(float(event["t"]), 3), "kind": "move", "x": fix_x, "y": fix_y}
            )
        result.append(event)
    # INVARIANT: 仅当实际注入了补偿定位时才重新校验，保持无滚轮时与旧实现一致
    # 的“只改坐标、不改结构”语义，避免对历史数据引入新的拒绝面。
    if fixes:
        errors = validate_edited_timeline(result)
        if errors:
            return EditResult(False, reason=errors[0])
    return EditResult(True, events=result)


def adjust_wait(
    events: list[dict[str, Any]],
    boundary: int,
    delta_seconds: float,
) -> EditResult:
    """调整一段空等：整体平移其后的事件，保留内部时长与同刻顺序。"""
    gaps = find_wait_gaps(events)
    gap = next((g for g in gaps if g.boundary == boundary), None)
    if gap is None:
        return EditResult(False, reason="找不到该等待区间")
    if not gap.editable:
        return EditResult(False, reason=gap.reason or "该间隔期间有输入按住，不能调整等待")
    try:
        delta = float(delta_seconds)
    except (TypeError, ValueError):
        return EditResult(False, reason="等待变化必须是数字")
    if not math.isfinite(delta):
        return EditResult(False, reason="等待变化必须是有限数字")
    updated = [dict(event) for event in events]
    for index in range(boundary, len(updated)):
        moment = float(updated[index]["t"]) + delta
        if moment < 0 or moment > MAX_TIMELINE_EVENT_SECONDS:
            return EditResult(False, reason="调整后事件时间超出允许范围")
        updated[index]["t"] = round(moment, 3)
    errors = validate_edited_timeline(updated)
    if errors:
        return EditResult(False, reason=errors[0])
    return EditResult(True, events=updated)


def delete_action(events: list[dict[str, Any]], group_id: str) -> EditResult:
    """删除一个完整动作（单击/按键）：含配对事件与确认关联的定位点。"""
    group = next((g for g in group_events(events) if g.id == group_id), None)
    if group is None:
        return EditResult(False, reason="找不到该动作")
    if not group.editable_delete:
        return EditResult(
            False, reason=group.reason or "仅支持完整识别的单击与按键整组删除"
        )
    drop = set(group.indices) | set(group.positioning_indices)
    fixes = _wheel_landing_fixes(events, drop)
    if fixes is None:
        return EditResult(False, reason="后续滚轮的原落点无法确定，未应用")
    updated: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if index in drop:
            continue
        if index in fixes:
            fix_x, fix_y = fixes[index]
            updated.append(
                {"t": round(float(event["t"]), 3), "kind": "move", "x": fix_x, "y": fix_y}
            )
        updated.append(dict(event))
    if not updated:
        return EditResult(False, reason="删除后时序为空，无法回放")
    errors = validate_edited_timeline(updated)
    if errors:
        return EditResult(False, reason=errors[0])
    return EditResult(True, events=updated)


def validate_edited_timeline(events: list[dict[str, Any]]) -> list[str]:
    """编辑后时序校验：复用时间/坐标/事件数/键鼠合法性，并补齐配对检查。

    返回按事件顺序排列的错误描述，空列表表示合法。每项含出错动作下标与原因。
    """
    errors: list[str] = []
    if not isinstance(events, (list, tuple)) or not events:
        return ["编辑后时序为空"]
    if len(events) > MAX_TIMELINE_EVENTS:
        return [f"编辑后事件数 {len(events)} 超过上限 {MAX_TIMELINE_EVENTS}"]
    previous = -1.0
    pressed: set[str] = set()
    buttons: set[str] = set()
    for index, item in enumerate(events, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 个事件格式无效")
            continue
        try:
            moment = float(item.get("t", -1))
        except (TypeError, ValueError):
            errors.append(f"第 {index} 个事件时间无效")
            continue
        if (
            not math.isfinite(moment)
            or moment < 0
            or moment > MAX_TIMELINE_EVENT_SECONDS
        ):
            errors.append(f"第 {index} 个事件时间超出 0～{MAX_TIMELINE_EVENT_SECONDS:g} 秒")
            continue
        if moment < previous:
            errors.append(f"第 {index} 个事件时间早于前一个事件，顺序已损坏")
        previous = moment
        kind = str(item.get("kind", "")).strip().lower()
        if kind == "key":
            action = str(item.get("action", "")).strip().lower()
            key = str(item.get("key", "")).strip().lower()
            if action not in _KEY_ACTIONS or not key:
                errors.append(f"第 {index} 个按键事件缺少键名或动作无效")
                continue
            if action == "down":
                if key in pressed:
                    errors.append(f"第 {index} 个事件重复按下 {key}，缺少中间松开")
                pressed.add(key)
            elif action == "up":
                if key not in pressed:
                    errors.append(f"第 {index} 个事件是孤立松开 {key}，此前没有按下")
                pressed.discard(key)
            elif key not in pressed:
                errors.append(f"第 {index} 个自动重复 {key} 此前没有按下")
        elif kind == "mouse":
            action = str(item.get("action", "")).strip().lower()
            button = str(item.get("button", "")).strip().lower()
            if action not in _MOUSE_ACTIONS or not button:
                errors.append(f"第 {index} 个鼠标事件缺少按钮或动作无效")
                continue
            if button not in MOUSE_BUTTON_NAMES:
                errors.append(f"第 {index} 个鼠标按钮 {button} 不受支持")
            if not _valid_point(item):
                errors.append(f"第 {index} 个鼠标事件坐标无效")
            if action == "down":
                if button in buttons:
                    errors.append(f"第 {index} 个事件重复按下鼠标 {button}")
                buttons.add(button)
            else:
                if button not in buttons:
                    errors.append(f"第 {index} 个事件是孤立松开鼠标 {button}")
                buttons.discard(button)
        elif kind == "move":
            if not _valid_point(item):
                errors.append(f"第 {index} 个移动事件坐标无效")
        elif kind == "wheel":
            try:
                if int(item.get("delta", 0)) == 0:
                    errors.append(f"第 {index} 个滚轮事件增量为 0")
            except (TypeError, ValueError):
                errors.append(f"第 {index} 个滚轮事件增量无效")
        else:
            errors.append(f"第 {index} 个事件类型不受支持")
    if buttons:
        errors.append(f"结束时仍有鼠标按钮未释放：{'、'.join(sorted(buttons))}")
    if pressed:
        errors.append(f"结束时仍有按键未释放：{'、'.join(sorted(pressed))}")
    return errors


def timeline_source_label(task: dict[str, Any] | None) -> str:
    """来源显示名：raw 显示“原样回放”，其余来源统一显示“输入时序”。

    playback_mode 只是来源标注，不参与回放；阶段一起所有 29.0 都共用编辑入口，
    摘要必须按真实来源显示，避免把普通录制生成的时序误标为原样回放。
    """
    mode = str(task.get("playback_mode", "")) if isinstance(task, dict) else ""
    return "原样回放" if mode == "raw" else "输入时序"


def summarize_edited_task(
    events: list[dict[str, Any]], source_label: str = "原样回放"
) -> tuple[str, float]:
    """编辑后的列表摘要与总时长，口径与录制保存一致。

    ``source_label`` 默认保持“原样回放”，兼容既有调用与测试；控制器按真实来源
    传入后，普通录制生成的时序不再被误标。
    """
    duration = round(max(0.0, float(events[-1]["t"])), 3) if events else 0.0
    return f"{source_label}：{len(events)} 个事件 / {duration:.2f} 秒", duration


@dataclass(frozen=True)
class DisplayRow:
    """一行步骤列表投影：只表达展示与可用操作，不持有第二份事件数据。

    ``group_ids`` 指回 ActionGroup.id，``indices`` 指回原始事件下标；折叠与展开
    只改变行的组织方式，写回仍以原始事件列表为准。
    """

    id: str
    kind: str  # wait / click / key / drag / wheel / move / unpaired / run_move / run_wheel
    label: str
    params: str
    t_start: float
    t_end: float
    indices: tuple[int, ...]
    group_ids: tuple[str, ...]
    capability: tuple[str, ...]  # detail / edit_pos / edit_wait / delete / preview / expand / collapse
    reason: str = ""
    wait_boundary: int | None = None
    expandable: bool = False
    expanded: bool = False
    parent_id: str = ""


def _wheel_direction(delta: object) -> str:
    """滚轮方向文案：负增量为向下，正增量为向上，0 视为未知。"""
    try:
        value = int(delta)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"
    if value > 0:
        return "向上"
    if value < 0:
        return "向下"
    return "未知"


def _row_from_group(group: ActionGroup, events: list[dict[str, Any]]) -> DisplayRow:
    """把一个动作分组投影成显示行；滚轮详情保留每个原始增量。"""
    capability = ["detail"]
    if group.editable_click:
        capability.append("edit_pos")
    if group.editable_delete:
        capability.append("delete")
    if group.kind in ("drag", "move", "wheel", "unpaired"):
        capability.append("preview")
    params = group.detail
    if group.kind == "wheel" and group.indices:
        deltas = [str(events[idx].get("delta")) for idx in group.indices]
        params = f"{_wheel_direction(events[group.indices[0]].get('delta'))}，{len(group.indices)} 条事件（delta={','.join(deltas)}）"
    return DisplayRow(
        id=group.id,
        kind=group.kind,
        label=group.label,
        params=params,
        t_start=group.t_start,
        t_end=group.t_end,
        indices=group.indices,
        group_ids=(group.id,),
        capability=tuple(capability),
        reason=group.reason,
    )


def build_display_rows(
    events: list[dict[str, Any]],
    *,
    expanded_ids: frozenset[str] = frozenset(),
    wait_threshold: float = 0.15,
) -> list[DisplayRow]:
    """把事件列表投影成行式步骤列表。

    连续自由移动、连续同方向滚轮折叠为一行并保留展开入口；按键、鼠标按钮等
    其它动作截断折叠组。开头空等（boundary=0）达到等待阈值时显示为等待行。
    展开时把折叠组内的每个成员作为子行返回，子行仍指回原始事件下标。
    """
    groups = group_events(events)
    rows: list[DisplayRow] = []
    if not groups:
        return rows

    gaps = find_wait_gaps(events)
    if gaps and gaps[0].boundary == 0 and gaps[0].duration >= wait_threshold:
        lead = gaps[0]
        rows.append(
            DisplayRow(
                id="wait:0",
                kind="wait",
                label="等待",
                params=f"开头空等 {lead.duration:.3f} 秒",
                t_start=lead.start,
                t_end=lead.end,
                indices=(),
                group_ids=(),
                capability=("detail", "edit_wait") if lead.editable else ("detail",),
                reason=lead.reason,
                wait_boundary=0,
            )
        )

    i = 0
    while i < len(groups):
        group = groups[i]
        if group.kind == "move":
            run: list[ActionGroup] = []
            while i < len(groups) and groups[i].kind == "move":
                run.append(groups[i])
                i += 1
            if len(run) == 1:
                rows.append(_row_from_group(run[0], events))
                continue
            fold_id = f"run_move:{run[0].id}"
            expanded = fold_id in expanded_ids
            indices = tuple(idx for item in run for idx in item.indices)
            rows.append(
                DisplayRow(
                    id=fold_id,
                    kind="run_move",
                    label=f"连续移动（{len(run)} 点）",
                    params=f"起点 → 终点 · {len(run)} 个采样点",
                    t_start=run[0].t_start,
                    t_end=run[-1].t_end,
                    indices=indices,
                    group_ids=tuple(item.id for item in run),
                    capability=("detail", "preview", "expand" if not expanded else "collapse"),
                    expandable=True,
                    expanded=expanded,
                )
            )
            if expanded:
                for item in run:
                    child = _row_from_group(item, events)
                    rows.append(
                        DisplayRow(
                            id=child.id,
                            kind=child.kind,
                            label=child.label,
                            params=child.params,
                            t_start=child.t_start,
                            t_end=child.t_end,
                            indices=child.indices,
                            group_ids=child.group_ids,
                            capability=child.capability,
                            reason=child.reason,
                            parent_id=fold_id,
                        )
                    )
            continue
        if group.kind == "wheel" and group.indices:
            direction = _wheel_direction(events[group.indices[0]].get("delta"))
            run = []
            while (
                i < len(groups)
                and groups[i].kind == "wheel"
                and groups[i].indices
                and _wheel_direction(events[groups[i].indices[0]].get("delta")) == direction
                and direction != "未知"
            ):
                run.append(groups[i])
                i += 1
            if len(run) == 1:
                # while 已推进 i；单条滚轮直接建行并 continue，不能再推进一次，
                # 否则跳过滚轮后的下一步（与 move 分支同口径）。
                rows.append(_row_from_group(run[0], events))
                continue
            fold_id = f"run_wheel:{run[0].id}"
            expanded = fold_id in expanded_ids
            deltas = [
                str(events[idx].get("delta"))
                for item in run
                for idx in item.indices
            ]
            indices = tuple(idx for item in run for idx in item.indices)
            rows.append(
                DisplayRow(
                    id=fold_id,
                    kind="run_wheel",
                    label=f"滚轮滑动（{len(run)} 条事件）",
                    params=f"{direction}，{len(indices)} 条事件（delta={','.join(deltas)}）",
                    t_start=run[0].t_start,
                    t_end=run[-1].t_end,
                    indices=indices,
                    group_ids=tuple(item.id for item in run),
                    capability=("detail", "preview", "expand" if not expanded else "collapse"),
                    expandable=True,
                    expanded=expanded,
                )
            )
            if expanded:
                for item in run:
                    child = _row_from_group(item, events)
                    rows.append(
                        DisplayRow(
                            id=child.id,
                            kind=child.kind,
                            label=child.label,
                            params=child.params,
                            t_start=child.t_start,
                            t_end=child.t_end,
                            indices=child.indices,
                            group_ids=child.group_ids,
                            capability=child.capability,
                            reason=child.reason,
                            parent_id=fold_id,
                        )
                    )
            continue
        rows.append(_row_from_group(group, events))
        i += 1
    return rows
