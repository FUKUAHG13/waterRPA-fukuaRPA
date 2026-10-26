# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Runtime prompts, overlays, recognition tests and coordinate previews."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QMessageBox, QToolTip

from ...constants import MAX_CLICK_INDICATOR_OVERLAYS
from ...log_policy import (
    LOG_ACTION,
    LOG_RECOGNITION,
)
from ...logging_service import write_log
from ...preview_model import (
    apply_preview_point_position,
    build_coordinate_preview,
    build_preview_line_segments,
    coordinate_preview_options,
    translate_preview_task_positions,
)
from ...privacy import private_error_text, safe_error_name
from ...profile_paths import resolve_task_image_paths
from ...run_config import EngineRunConfig, RunConfigError
from ...task_model import parse_coordinate_text
from ..input_tools import CoordinatePickerUI
from ..lifecycle import is_runtime_editing_locked, safe_close

if TYPE_CHECKING:
    from .host_view import HostView


logger = logging.getLogger(__name__)


def _overlays():
    """Return the overlay layer, importing it on first use (ADR 0041).

    启动期不需要该层；失败时记 critical 留痕并原样抛出，不吞异常、不缓存失败，
    因此下一次调用会重新尝试导入。
    """

    try:
        from .. import overlays
    except Exception as error:  # noqa: BLE001 - 留痕后原样抛出
        logger.critical("浮层导入失败：%s", error)
        raise
    return overlays


class RecognitionTestBridge(QObject):
    """Deliver a background recognition result back to the Qt UI thread."""

    completed = Signal(object)


class VisualFeedbackController:
    """Own short-lived visual feedback windows created by the main UI."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.recognition_test_bridge = RecognitionTestBridge(host)
        self.recognition_test_bridge.completed.connect(
            self.on_task_image_recognition_finished
        )
        self.recognition_test_thread = None
        self.recognition_test_cancel = None
        self.recognition_test_active = False
        self.recognition_test_row = None
        self.recognition_test_overlay = None
        self.running_overlay = None
        self.click_indicator_overlays = []
        self.mouse_trail_overlay = None
        self.all_points_preview = None
        self.all_points_preview_plan = None
        self.active_script_prompt = None
        self.deferred_running_status = None

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已销毁")
        return host

    def show_running_status_overlay(self, position_name, activity_name="脚本"):
        overlays = _overlays()

        if self.running_overlay is None:
            self.running_overlay = overlays.RunningStatusOverlay()
        self.running_overlay.start_overlay(position_name, activity_name)

    def update_running_status_overlay(self, data):
        if self.host._live_window_interaction:
            self.deferred_running_status = dict(data or {})
            self.host.ui_performance.increment(
                "ui.live_interaction.status_deferred"
            )
            return
        if self.running_overlay and self.running_overlay.isVisible():
            self.running_overlay.set_status(data)

    def flush_deferred_running_status(self):
        pending = self.deferred_running_status
        self.deferred_running_status = None
        if pending is not None:
            self.update_running_status_overlay(pending)

    def hide_running_status_overlay(self, activity_name=None):
        if self.running_overlay and (
            activity_name is None
            or self.running_overlay.activity_name == str(activity_name)
        ):
            self.running_overlay.stop_overlay()

    def show_script_prompt(self, request):
        if getattr(request, "done", None) is None or request.done.is_set():
            return
        self.cancel_active_script_prompt()
        message = QMessageBox(self.host)
        message.setWindowTitle("脚本提醒")
        message.setIcon(QMessageBox.Information)
        message.setTextFormat(Qt.PlainText)
        message.setText(str(getattr(request, "text", "")))
        message.setStandardButtons(QMessageBox.Ok)
        message.setModal(False)

        # L-35: 40ms 轮询用于检测脚本取消信号。该间隔在取消响应速度（用户感知
        # 延迟 < 50ms）和 CPU 开销之间取得平衡，是 Qt 定时器驱动的取消检测的
        # 合理选择；更短的间隔不会带来可感知的用户体验提升。
        poll_timer = QTimer(message)
        poll_timer.setInterval(40)

        def finish_prompt(_result=0):
            poll_timer.stop()
            if not request.done.is_set():
                request.acknowledge()
            current = self.active_script_prompt
            if current and current[0] is message:
                self.active_script_prompt = None

        def poll_cancelled():
            if request.done.is_set() and getattr(request, "cancelled", False):
                message.close()

        message.finished.connect(finish_prompt)
        poll_timer.timeout.connect(poll_cancelled)
        self.active_script_prompt = (message, request, poll_timer)
        poll_timer.start()
        message.open()

    def cancel_active_script_prompt(self):
        current = self.active_script_prompt
        if not current:
            return
        message, request, poll_timer = current
        self.active_script_prompt = None
        try:
            request.cancel()
        except Exception as exc:
            logger.debug("取消反馈请求失败: %s", exc, exc_info=True)
            pass
        try:
            poll_timer.stop()
            message.close()
        except RuntimeError:
            pass

    def show_click_indicator_overlay(self, data):
        overlays = _overlays()

        try:
            if str((data or {}).get("kind", "")) == "mouse_trail":
                if self.mouse_trail_overlay is None:
                    self.mouse_trail_overlay = overlays.MouseTrailOverlay()
                    self.mouse_trail_overlay.destroyed.connect(
                        lambda *_args: setattr(self, "mouse_trail_overlay", None)
                    )
                self.mouse_trail_overlay.add_points(
                    (data or {}).get("points", ()),
                    new_path=bool((data or {}).get("new_path", False)),
                )
                return
            while (
                len(self.click_indicator_overlays)
                >= MAX_CLICK_INDICATOR_OVERLAYS
            ):
                oldest = self.click_indicator_overlays.pop(0)
                safe_close(oldest)
            overlay = overlays.ClickPointOverlay(
                data.get("x", 0),
                data.get("y", 0),
                data.get("text", ""),
            )
            self.click_indicator_overlays.append(overlay)
            overlay.destroyed.connect(
                lambda *_args, current=overlay: (
                    self.click_indicator_overlays.remove(current)
                    if current in self.click_indicator_overlays
                    else None
                )
            )
        except Exception as error:
            write_log(
                f"显示点击位置提示失败: {safe_error_name(error)}",
                private_message=(
                    f"显示点击位置提示失败: {private_error_text(error)}"
                ),
                         category=LOG_ACTION,
                         critical=True,
                     )

    def test_task_image_recognition(self, task_row):
        host = self.host
        if not host.runtime_ready:
            QToolTip.showText(
                QCursor.pos(), "识别运行库仍在准备，请稍候", task_row
            )
            return
        if host.run_controller.is_active:
            QToolTip.showText(
                QCursor.pos(), "脚本运行期间不能单独测试识别", task_row
            )
            return
        if self.recognition_test_active:
            QToolTip.showText(
                QCursor.pos(), "已有一项识别测试正在进行", task_row
            )
            return

        try:
            task = resolve_task_image_paths([task_row.get_data()], host.base_dir)[0]
        except ValueError as error:
            QMessageBox.warning(host, "无法测试识别", str(error))
            return
        config = host.get_current_ui_config()
        try:
            EngineRunConfig.from_mapping(
                config, len(config.get("tasks", []))
            ).apply_to(host.engine)
        except RunConfigError as error:
            QMessageBox.warning(host, "无法测试识别", str(error))
            return

        self.recognition_test_active = True
        self.recognition_test_row = weakref.ref(task_row)
        task_row.recognition_test_btn.setEnabled(False)
        host.start_btn.setEnabled(False)
        host.single_step_btn.setEnabled(False)

        host_ref = weakref.ref(host)
        cancel_event = threading.Event()
        self.recognition_test_cancel = cancel_event

        def recognize():
            h = host_ref()
            if h is None:
                return
            if cancel_event.is_set():
                return
            started = time.perf_counter()
            try:
                result = h.engine.recognize_task_once(task)
                result["error"] = ""
            except Exception as error:
                result = {
                    "found": False,
                    "error": str(error),
                    "error_type": safe_error_name(error),
                }
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000.0
            if cancel_event.is_set():
                return
            with contextlib.suppress(RuntimeError):
                self.recognition_test_bridge.completed.emit(result)

        self.recognition_test_thread = threading.Thread(
            target=recognize,
            daemon=True,
            name="fukuaRPA-recognition-test",
        )
        self.recognition_test_thread.start()

    def on_task_image_recognition_finished(self, result):
        host = self.host
        self.recognition_test_active = False
        self.recognition_test_thread = None
        self.recognition_test_cancel = None
        row_reference = self.recognition_test_row
        self.recognition_test_row = None
        row = row_reference() if row_reference else None
        if row is not None:
            with contextlib.suppress(RuntimeError):
                row.recognition_test_btn.setEnabled(True)
        if host.runtime_ready and not host.run_controller.is_active:
            host.start_btn.setEnabled(True)
            host.single_step_btn.setEnabled(True)

        error = str((result or {}).get("error", "") or "")
        if error:
            write_log(
                "图片识别测试失败："
                f"{(result or {}).get('error_type', 'RecognitionTestError')}",
                         category=LOG_RECOGNITION,
                         critical=True,
                     )
            QMessageBox.warning(host, "识别测试失败", error)
            return
        if not (result or {}).get("found"):
            QMessageBox.information(
                host,
                "未识别到目标",
                "本次测试没有找到符合当前相似度、缩放范围和识别区域的目标。\n\n"
                f"本次识别耗时 {(result or {}).get('elapsed_ms', 0.0):.1f} ms。",
            )
            return

        # INVARIANT: 首次导入失败不能阻断识别收尾或提前关闭已有浮层（ADR 0041）。
        overlays = _overlays()
        previous = self.recognition_test_overlay
        safe_close(previous)
        overlay = overlays.RecognitionMatchOverlay(
            result["center_x"],
            result["center_y"],
            result["matched_width"],
            result["matched_height"],
            result["click_x"],
            result["click_y"],
            score=result["score"],
            scale=result["scale"],
        )
        self.recognition_test_overlay = overlay
        overlay.destroyed.connect(
            lambda *_args, current=overlay: setattr(
                self,
                "recognition_test_overlay",
                None
                if self.recognition_test_overlay is current
                else self.recognition_test_overlay,
            )
        )
        write_log(
            "图片识别测试命中："
            f"相似度 {result['score']:.3f}，缩放 {result['scale']:.3f}x，"
            f"耗时 {result.get('elapsed_ms', 0.0):.1f} ms。",
                     category=LOG_RECOGNITION,
                 )

    @staticmethod
    def coordinate_preview_options_from_task(task):
        return coordinate_preview_options(task)

    def collect_coordinate_click_preview_points(self, max_points=800):
        tasks = self.host.snapshot_tasks()
        plan = build_coordinate_preview(tasks, max_points=max_points)
        return (
            list(plan.points),
            plan.truncated,
            list(plan.labels),
            list(plan.line_segments),
        )

    @staticmethod
    def preview_line_segments(step_groups, internal_segments=None):
        return build_preview_line_segments(step_groups, internal_segments)

    def show_all_coordinate_click_preview(self):
        overlays = _overlays()

        tasks = self.host.snapshot_tasks()
        plan = build_coordinate_preview(tasks, max_points=800)
        if not plan.points:
            QMessageBox.information(
                self.host,
                "无法预览",
                "当前脚本没有可静态预览的坐标步骤。\n图片识别点击不会参与此预览。",
            )
            return
        self.close_all_coordinate_click_preview()
        self.all_points_preview_plan = plan
        suffix = "，已截断前 800 个且连线暂不可拖动" if plan.truncated else ""
        title = (
            f"坐标总预览：{len(plan.points)} 个点位{suffix}；右键/Esc 关闭"
        )
        editable_indices = [
            index
            for index, source in enumerate(plan.point_sources)
            if source.editable
        ]
        self.all_points_preview = overlays.CoordinateStepPreviewOverlay(
            plan.points,
            {"direction": "全部坐标点击"},
            title=title,
            auto_close_ms=0,
            draw_lines=True,
            detail_text=(
                "拖动直接坐标或自定义点位圆点可单独修正；拖动组内连线会整体平移该组；"
                "跨组连线只展示。"
            ),
            close_on_left_blank=False,
            editable_indices=editable_indices,
            point_labels=plan.labels,
            line_segments=plan.line_segments,
        )
        self.all_points_preview.point_released_callback = (
            self.on_all_points_preview_point_released
        )
        self.all_points_preview.line_released_callback = (
            self.on_all_points_preview_line_released
        )
        preview = self.all_points_preview
        preview.destroyed.connect(
            lambda *_args, current=preview: self.clear_all_coordinate_click_preview(
                current
            )
        )

    def _refresh_all_points_preview_plan(self):
        plan = build_coordinate_preview(self.host.snapshot_tasks(), max_points=800)
        self.all_points_preview_plan = plan
        overlay = self.all_points_preview
        if overlay is not None:
            overlay.set_editable_indices(
                index
                for index, source in enumerate(plan.point_sources)
                if source.editable
            )
            overlay.set_point_labels(plan.labels)
            overlay.set_line_segments(plan.line_segments)
        return list(plan.points)

    def on_all_points_preview_point_released(self, index, x, y):
        plan = self.all_points_preview_plan
        if (
            plan is None
            or is_runtime_editing_locked(self.host)
            or not 0 <= int(index) < len(plan.point_sources)
        ):
            return self._refresh_all_points_preview_plan()
        before = self.host.snapshot_tasks()
        after = apply_preview_point_position(
            before,
            plan.point_sources[int(index)],
            (x, y),
        )
        self.host.apply_task_snapshot_changes(
            after,
            "坐标总预览单点拖动",
        )
        return self._refresh_all_points_preview_plan()

    def on_all_points_preview_line_released(self, segment_index, dx, dy):
        plan = self.all_points_preview_plan
        if (
            plan is None
            or is_runtime_editing_locked(self.host)
            or not 0 <= int(segment_index) < len(plan.line_segments)
        ):
            return self._refresh_all_points_preview_plan()
        segment = plan.line_segments[int(segment_index)]
        task_refs = segment.get("move_task_refs", ())
        if not task_refs:
            return self._refresh_all_points_preview_plan()
        before = self.host.snapshot_tasks()
        after = translate_preview_task_positions(before, task_refs, dx, dy)
        self.host.apply_task_snapshot_changes(
            after,
            "坐标总预览分组平移",
        )
        return self._refresh_all_points_preview_plan()

    def clear_all_coordinate_click_preview(self, preview=None):
        if preview is not None and self.all_points_preview is not preview:
            return
        self.all_points_preview = None
        self.all_points_preview_plan = None

    def close_all_coordinate_click_preview(self):
        preview = self.all_points_preview
        safe_close(preview)
        self.all_points_preview = None
        self.all_points_preview_plan = None

    def close_all(self):
        self.cancel_active_script_prompt()
        # R-5: signal the recognition test thread to stop so it does not emit a
        # result into a tearing-down UI. The thread checks this event before
        # doing work and before delivering its result.
        if self.recognition_test_cancel is not None:
            self.recognition_test_cancel.set()
        self.close_all_coordinate_click_preview()
        for overlay in list(self.click_indicator_overlays):
            safe_close(overlay)
        self.click_indicator_overlays.clear()
        for overlay in (
            self.recognition_test_overlay,
            self.running_overlay,
            self.mouse_trail_overlay,
        ):
            if overlay is None:
                continue
            safe_close(overlay)
        self.recognition_test_overlay = None
        self.running_overlay = None
        self.mouse_trail_overlay = None

    # ------------------------------------------------------------------
    # Dodge point coordinate picking (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def start_dodge_point_pick(self, point_index):
        host = self.host
        point_index = 2 if int(point_index) == 2 else 1
        previous = host.dodge_pickers.pop(point_index, None)
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                previous.close()
        picker = CoordinatePickerUI(
            "point",
            lambda value, index=point_index: self.on_dodge_point_picked(
                index, value
            ),
        )
        host.dodge_pickers[point_index] = picker
        picker.destroyed.connect(
            lambda *_args, index=point_index: host.dodge_pickers.pop(
                index, None
            )
        )

    def on_dodge_point_picked(self, point_index, value):
        host = self.host
        point = parse_coordinate_text(value)
        if point is None:
            return
        x_edit, y_edit = (
            (host.dodge_x2, host.dodge_y2)
            if int(point_index) == 2
            else (host.dodge_x1, host.dodge_y1)
        )
        x_edit.setText(str(point[0]))
        y_edit.setText(str(point[1]))
        host.log_setting_change(
            f"避让坐标{2 if int(point_index) == 2 else 1}",
            f"{point[0]},{point[1]}",
        )
        host.restore_settings_dialog_focus()
