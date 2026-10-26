"""Single source of truth for enumerating image paths referenced by a task.

A workflow step can reference template images in five places:

* ``value`` — when the command accepts an image target and the value is not a
  plain coordinate;
* ``until_cond{1..3}_image`` — the "until condition" template images;
* ``drag_target_img`` — the optional drag end-point template image;
* ``drag_source_img`` — the optional drag start-point template image;
* ``multi_image_actions[].image`` — each rule of a multi-image action.

These fields were previously enumerated independently in several modules
(workflow security review, portable path resolution and package import/export).
This module centralizes that enumeration so the privacy-sensitive set of image
fields cannot drift apart.

It deliberately depends only on ``commands`` and ``task_model`` (both of which
are lower-level), never on the modules that consume it, so importing it cannot
create a cycle. ``task_model`` itself cannot host this helper because it would
then have to import ``commands``, which already imports ``task_model``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .commands import IMAGE_TARGET_COMMANDS
from .task_model import parse_coordinate_text


def iter_task_image_refs(
    task: Mapping[str, Any],
) -> Iterator[tuple[str, Any, str, str]]:
    """Yield ``(kind, container, key, path)`` for every image field of *task*.

    * ``kind`` is one of ``"value"``, ``"condition"``, ``"drag_target"``,
      ``"drag_source"`` or ``"multi_image"``;
    * ``container`` is the mapping that owns the field (the task itself, or a
      multi-image rule) so callers can write a resolved/rewritten path back;
    * ``key`` is the field name inside ``container``;
    * ``path`` is the stripped, non-empty image path string.

    Only fields that actually hold an image path are yielded: the ``value``
    field is included only when the command accepts images and the value is not
    a coordinate; condition, drag-target and multi-image fields are included
    only when non-empty after stripping. The yield order is ``value``, then the
    three condition fields, then the drag-target field, then the multi-image
    rules in list order.
    """

    if not isinstance(task, Mapping):
        return
    try:
        command = float(task.get("type", 0))
    except (TypeError, ValueError):
        command = 0.0
    value = str(task.get("value", "") or "").strip()
    if command in IMAGE_TARGET_COMMANDS and value and not parse_coordinate_text(value):
        yield ("value", task, "value", value)
    for index in range(1, 4):
        key = f"until_cond{index}_image"
        image = str(task.get(key, "") or "").strip()
        if image:
            yield ("condition", task, key, image)
        # 覆盖匹配小图（until_cond{index}_overlay_image）：与 until_cond{index}_image
        # 同为条件引用的模板图，必须一并枚举，否则打包资产收集、隐私脱敏、
        # 可移植路径解析等消费者会遗漏它而静默丢弃该引用。
        overlay_key = f"until_cond{index}_overlay_image"
        overlay = str(task.get(overlay_key, "") or "").strip()
        if overlay:
            yield ("condition", task, overlay_key, overlay)
    drag_target = str(task.get("drag_target_img", "") or "").strip()
    if drag_target:
        yield ("drag_target", task, "drag_target_img", drag_target)
    drag_source = str(task.get("drag_source_img", "") or "").strip()
    if drag_source:
        yield ("drag_source", task, "drag_source_img", drag_source)
    rules = task.get("multi_image_actions", [])
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            image = str(rule.get("image", "") or "").strip()
            if image:
                yield ("multi_image", rule, "image", image)


def iter_task_image_paths(task: Mapping[str, Any]) -> Iterator[str]:
    """Yield just the image path strings referenced by *task*, in field order."""

    for _kind, _container, _key, path in iter_task_image_refs(task):
        yield path
