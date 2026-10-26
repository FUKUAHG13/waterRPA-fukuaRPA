"""Data model for the advanced first-matching-image action."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .constants import MAX_MULTI_IMAGE_ACTION_RULES


@dataclass(frozen=True)
class MultiImageChildAction:
    """Immutable descriptor for one child action selectable in a multi-image rule.

    Attributes:
        code: Stable machine identifier persisted in profile data.
        label: Localized display label shown in the UI.
        value_hint: Placeholder hint for the optional value field; empty when
            the action takes no value.
    """

    code: str
    label: str
    value_hint: str = ""


MULTI_IMAGE_CHILD_ACTIONS = (
    MultiImageChildAction("left_click", "匹配点左键单击"),
    MultiImageChildAction("left_double_click", "匹配点左键双击"),
    MultiImageChildAction("right_click", "匹配点右键单击"),
    MultiImageChildAction("hover", "匹配点鼠标悬停"),
    MultiImageChildAction("text", "输入文本", "要输入的文字"),
    MultiImageChildAction("hotkey", "系统按键", "例如 Ctrl+C"),
    MultiImageChildAction("wait", "等待", "秒数，例如 0.5"),
    MultiImageChildAction("scroll", "滚轮滑动", "正数向上，负数向下"),
    MultiImageChildAction("prompt", "弹窗提醒", "提示文字"),
    MultiImageChildAction("sound", "声音提示", "日志备注（可选）"),
    MultiImageChildAction("stop", "停止运行", "停止备注（可选）"),
)

MULTI_IMAGE_ACTION_BY_CODE = {
    action.code: action for action in MULTI_IMAGE_CHILD_ACTIONS
}
MULTI_IMAGE_ACTION_BY_LABEL = {
    action.label: action for action in MULTI_IMAGE_CHILD_ACTIONS
}
MULTI_IMAGE_POINT_ACTIONS = frozenset(
    {"left_click", "left_double_click", "right_click", "hover"}
)
MULTI_IMAGE_REQUIRED_VALUE_ACTIONS = frozenset({"text", "hotkey", "wait", "scroll"})


def normalize_multi_image_action_code(value: Any) -> str:
    """Map a stored code or localized label to the canonical action code.

    Accepts either the stable ``code`` or the display ``label`` so legacy data
    that persisted labels keeps working. Unknown input is returned verbatim
    (stripped) so downstream validation can flag it instead of silently
    coercing it to a valid action.

    Args:
        value: Raw action code or label from profile data.

    Returns:
        The canonical action code, or the stripped input when unrecognized.
    """
    text = str(value or "").strip()
    if text in MULTI_IMAGE_ACTION_BY_CODE:
        return text
    action = MULTI_IMAGE_ACTION_BY_LABEL.get(text)
    return action.code if action else text


def normalize_multi_image_rules(value: Any) -> list[dict[str, Any]]:
    """Return a detached, bounded list of normalized rules.

    Invalid entries are dropped, the list is capped at
    ``MAX_MULTI_IMAGE_ACTION_RULES``, and field values are coerced to stripped
    strings. Action codes are canonicalized via
    ``normalize_multi_image_action_code``; an unrecognized action is preserved
    so validation can report it rather than being silently rewritten.

    Args:
        value: Raw ``multi_image_actions`` field from a task dict.

    Returns:
        A new list of rule dicts with ``image``, ``action``, ``value``,
        ``region`` and ``confidence`` keys (plus ``cache_key`` when present).
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rules = []
    for raw in value[:MAX_MULTI_IMAGE_ACTION_RULES]:
        if not isinstance(raw, Mapping):
            continue
        rule = {
            "image": str(raw.get("image", "") or "").strip(),
            "action": normalize_multi_image_action_code(raw.get("action", "left_click")),
            "value": str(raw.get("value", "") or ""),
            "region": str(raw.get("region", "") or "").strip(),
            "confidence": str(raw.get("confidence", "") or "").strip(),
        }
        cache_key = str(raw.get("cache_key", "") or "").strip()
        if cache_key:
            rule["cache_key"] = cache_key
        rules.append(rule)
    return rules


def multi_image_action_summary(value: Any) -> str:
    """Build a short human-readable summary of a multi-image rule set.

    Args:
        value: Raw ``multi_image_actions`` field from a task dict.

    Returns:
        A single-line summary such as the rule count and how many rules have a
        configured image, or a placeholder when no rules exist.
    """
    rules = normalize_multi_image_rules(value)
    if not rules:
        return "尚未添加图片动作规则"
    configured = sum(1 for rule in rules if rule["image"])
    return f"按顺序检查 {len(rules)} 条规则，已选择 {configured} 张图片"


# retained (P7-1): no in-repo callers; public label/value helper API surface.
def multi_image_action_label(code: Any) -> str:
    """Return the localized display label for an action code.

    Args:
        code: Action code or label accepted by
            ``normalize_multi_image_action_code``.

    Returns:
        The display label, or ``"未知动作"`` when the code is unrecognized.
    """
    action = MULTI_IMAGE_ACTION_BY_CODE.get(normalize_multi_image_action_code(code))
    return action.label if action else str(code or "未知动作")


def multi_image_action_value_hint(code: Any) -> str:
    """Return the value-field placeholder hint for an action code.

    Args:
        code: Action code or label accepted by
            ``normalize_multi_image_action_code``.

    Returns:
        The value hint, or an empty string when the action takes no value or
        the code is unrecognized.
    """
    action = MULTI_IMAGE_ACTION_BY_CODE.get(normalize_multi_image_action_code(code))
    return action.value_hint if action else ""
