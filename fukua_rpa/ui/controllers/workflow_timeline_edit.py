# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""录制的行内编辑入口：打开步骤草稿、校验并一次写回。

本模块提供纯函数，由 ``WorkflowContextMixin`` 的两个薄方法调用，避免向
``WorkflowEditorController`` 的锁定 MRO 增加新的 mixin 基类。
"""
from __future__ import annotations

import logging

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QAbstractItemView, QToolTip

from ...constants import TASK_TYPE_INPUT_TIMELINE
from ...log_policy import LOG_PARAMETERS
from ...timeline_editor import (
    summarize_edited_task,
    timeline_source_label,
    validate_edited_timeline,
)
from ..lifecycle import is_runtime_editing_locked, widget_is_alive

logger = logging.getLogger(__name__)


def is_timeline_task(task):
    """统一的输入时序识别：只看 29.0 类型与实际事件数据，不依赖来源标注。"""
    if not isinstance(task, dict):
        return False
    try:
        raw_type = float(task.get("type", -1))
    except (TypeError, ValueError):
        return False
    return (
        raw_type == TASK_TYPE_INPUT_TIMELINE
        and isinstance(task.get("input_events"), list)
    )


def timeline_edit_status(task):
    """编辑资格与来源的纯判定，右键菜单、控制器与写回共用同一规则。

    返回 ``(editable, viewable, source_label, reason)``：类型或载荷结构不合法时
    不可查看也不可编辑；空时序、非法格式或脱敏清空的数据不可编辑，但可查看的
    结构性问题会给出原因，由 UI 提示用户。``playback_mode`` 仅作来源标注。
    """
    if not is_timeline_task(task):
        return False, False, "输入时序", "该步骤不是输入时序录制"
    events = task.get("input_events")
    if not events:
        return False, False, timeline_source_label(task), "时序为空，无法查看或编辑"
    errors = validate_edited_timeline(events)
    if errors:
        # 结构非法：不进入写回；原因透传给 UI 作为只读查看提示。
        return False, True, timeline_source_label(task), errors[0]
    return True, True, timeline_source_label(task), ""


def is_editable_timeline_task(task):
    """右键入口资格：合法可编辑的 29.0 时序（所有来源一致）。"""
    return timeline_edit_status(task)[0]


def is_viewable_timeline_task(task):
    """可查看资格：结构非法的 29.0 也可打开查看原因，但不提供写回。"""
    return timeline_edit_status(task)[1]


def is_raw_timeline_task(task):
    """兼容别名：仅识别 raw 来源的合法时序。

    阶段一起右键与控制器改用 ``is_editable_timeline_task``；本函数保留旧窄语义，
    供既有调用与历史测试继续使用，不参与新入口判定。
    """
    if not is_timeline_task(task):
        return False
    return str(task.get("playback_mode", "")) == "raw" and timeline_edit_status(task)[0]


def edit_timeline_recording(controller, row=None):
    """在实际的工作流步骤中打开录制草稿。"""
    host = controller.host
    if is_runtime_editing_locked(host) or host.start_in_progress:
        QToolTip.showText(QCursor.pos(), "脚本正在启动或运行，无法编辑录制", host)
        return False
    rows = controller.selected_row_indexes() if row is None else [int(row)]
    if len(rows) != 1:
        QToolTip.showText(QCursor.pos(), "请只选中一个输入时序步骤", host)
        return False
    task_list = host.task_list
    item = task_list.item(rows[0])
    if item is None or item.isHidden():
        return False

    active_item = task_list.active_timeline_editor_item()
    active_row = task_list.itemWidget(active_item) if active_item is not None else None
    active_editor = active_row.timeline_editor if active_row is not None else None
    if active_editor is not None and item is active_item:
        if active_editor.isVisible():
            task_list.scrollToItem(item, QAbstractItemView.PositionAtTop)
            active_editor.event_list.setFocus()
            return True
        return False

    source_task = controller._task_data_at(rows[0])
    editable, _viewable, source_label, status_reason = timeline_edit_status(source_task)
    if not editable:
        QToolTip.showText(QCursor.pos(), status_reason, host)
        return False
    if active_editor is not None and active_editor.has_changes():
        QToolTip.showText(QCursor.pos(), "请先应用或取消当前录制草稿", host)
        return False
    row_widget = host.ensure_task_item_widget(item)
    if row_widget is None or row_widget.controls_widget.isHidden():
        return False

    # 按需导入：启动期不加载录制编辑面板（ADR 0040/0041 同口径）。
    try:
        from ..timeline_editor_dialog import TimelineEditorPanel
    except Exception as error:  # noqa: BLE001 - 如实记录并中止
        logger.critical("录制编辑面板导入失败：%s", error)
        return False

    profile_name = host.current_profile_name
    panel = TimelineEditorPanel(source_task)

    def is_active():
        return (
            widget_is_alive(task_list)
            and task_list.active_timeline_editor_item() is item
            and task_list.itemWidget(item) is row_widget
            and row_widget.timeline_editor is panel
        )

    def cancel():
        if is_active():
            task_list.close_timeline_editor()

    def apply(events):
        if not is_active():
            return
        if is_runtime_editing_locked(host) or host.start_in_progress:
            panel.status.setText("脚本正在启动或运行，未应用录制草稿")
            return
        # SAFETY: 按当前列表项定位；方案切换或事件已被替换时拒绝旧草稿写回。
        index = task_list.row(item)
        current = host.snapshot_tasks()
        latest = current[index]
        if (
            host.current_profile_name != profile_name
            or latest.get("step_id") != source_task.get("step_id")
            or not is_timeline_task(latest)
            or latest.get("input_events") != source_task.get("input_events")
        ):
            panel.status.setText("原步骤已变化。请取消草稿后重新展开。")
            return
        if events == latest["input_events"]:
            task_list.close_timeline_editor()
            return
        errors = validate_edited_timeline(events)
        if errors:
            panel.status.setText(f"时序无效，未应用：{errors[0]}")
            return

        summary, duration = summarize_edited_task(events, source_label)
        current[index] = dict(
            latest, input_events=events, recorded_duration=duration, value=summary
        )
        if host.apply_task_snapshot_changes(current, "编辑录制"):
            task_list.close_timeline_editor()
            # 日志只写数量与时长，不写键鼠载荷。
            host.append_application_log(
                f"第 {index + 1} 步录制已编辑：{len(events)} 个事件 / {duration:.2f} 秒。",
                LOG_PARAMETERS,
            )

    panel.cancelRequested.connect(cancel)
    panel.applyRequested.connect(apply)
    task_list.close_timeline_editor()
    task_list.pin_timeline_editor(item)
    row_widget.mount_timeline_editor(panel)
    host.apply_ui_scale_to_widget(panel)
    task_list.scrollToItem(item, QAbstractItemView.PositionAtTop)
    panel.event_list.setFocus()
    return True
