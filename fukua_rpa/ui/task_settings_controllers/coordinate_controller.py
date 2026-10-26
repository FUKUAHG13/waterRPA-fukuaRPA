# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Coordinate sequence, stepping and preview interactions for step settings."""

from PySide6.QtCore import QRect
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QMessageBox, QToolTip

from ...preview_model import _clamped_int
from ...task_model import (
    build_coord_step_positions,
    parse_coordinate_sequence,
    parse_coordinate_text,
    parse_float_text,
    serialize_coordinate_sequence,
)
from ..input_tools import CoordinatePickerUI, MultiPointPickerUI
from ..lifecycle import safe_close
from ..overlays import CoordinateStepPreviewOverlay


class CoordinateSettingsController:
    def __init__(self, dialog):
        self.dialog = dialog

    def update_step_ui(self, _=None):
        host = self.dialog
        sequence_enabled = (
            getattr(host, "coord_sequence_chk", None)
            and host.coord_sequence_chk.isChecked()
        )
        enabled = (
            host.coordinate_step_available
            and host.coord_step_chk.isChecked()
            and not sequence_enabled
        )
        direction = host.coord_step_direction_combo.currentText()
        target_mode = direction == "移动到新点位"
        for widget in [
            host.coord_step_every_edit,
            host.coord_step_direction_combo,
            host.coord_step_distance_edit,
            host.coord_step_dx_edit,
            host.coord_step_dy_edit,
            host.coord_step_point_edit,
            host.coord_step_max_steps_edit,
            host.coord_step_max_distance_edit,
            host.coord_step_stop_chk,
            host.coord_step_reset_after_edit,
            host.coord_step_pick_btn,
            host.coord_step_preview_btn,
        ]:
            widget.setEnabled(enabled)
        # FR-20260812 建议-架构-1: _coord_direction_locked_for_simple 恒为 False
        # （无任何置 True 写点），属死状态，直接按 enabled 控制。
        host.coord_step_direction_combo.setEnabled(enabled)
        host.coord_step_distance_edit.setEnabled(
            enabled and direction in ["向上", "向下", "向左", "向右"]
        )
        host.coord_step_dx_edit.setEnabled(enabled and direction == "自定义偏移")
        host.coord_step_dy_edit.setEnabled(enabled and direction == "自定义偏移")
        host.coord_step_point_edit.setEnabled(enabled and direction == "移动到新点位")
        host.coord_step_pick_btn.setEnabled(enabled and direction == "移动到新点位")
        host.coord_step_max_distance_edit.setEnabled(enabled and not target_mode)
        host.coord_step_max_distance_label.setEnabled(enabled and not target_mode)
        host.coord_step_distance_limit_group.setVisible(not target_mode)
        host.coord_step_max_steps_label.setText(
            "路径点位数:" if target_mode else "最多偏移次数:"
        )
        host.coord_step_stop_chk.setText(
            "到达目标点后停止脚本"
            if target_mode
            else "达到移动上限后停止脚本"
        )
        limit_label = host.coordinate_target_form.labelForField(
            host.coord_limit_row
        )
        if limit_label is not None:
            limit_label.setText("路径点位:" if target_mode else "移动上限:")
        host.coord_step_preview_btn.setEnabled(
            enabled and host.base_coordinate is not None
        )
        self.update_manual_ui()

    def update_sequence_ui(self, _=None):
        host = self.dialog
        enabled = host.coordinate_step_available and host.coord_sequence_chk.isChecked()
        host.coord_sequence_chk.setEnabled(host.coordinate_step_available)
        host.coord_sequence_details.setVisible(enabled)
        for widget in [
            host.coord_sequence_text,
            host.coord_sequence_end_combo,
            host.coord_sequence_batch_combo,
            host.coord_sequence_batch_count_edit,
            host.coord_sequence_pick_btn,
            host.coord_sequence_preview_btn,
            host.coord_sequence_clear_btn,
        ]:
            widget.setEnabled(enabled)
        host.coord_sequence_batch_count_edit.setEnabled(
            enabled and host.coord_sequence_batch_combo.currentData() == "count"
        )
        host.coord_step_chk.setToolTip(
            "已启用自定义点位，坐标步进会自动忽略。关闭自定义点位后可继续使用坐标步进。"
            if enabled
            else "仅对直接输入坐标的点击步骤生效；图片识别点击会自动忽略。"
        )
        host.coord_sequence_box.updateGeometry()
        host.form_widget.updateGeometry()
        host.update_repeat_ui()
        self.update_step_ui()

    def sequence_points(self):
        return parse_coordinate_sequence(self.dialog.coord_sequence_text.toPlainText())

    def append_sequence_point(self, value):
        host = self.dialog
        point = parse_coordinate_text(value)
        if not point:
            return
        points = self.sequence_points()
        points.append(point)
        host.coord_sequence_text.setPlainText(serialize_coordinate_sequence(points))
        self._refresh_live_sequence_preview()

    def _refresh_live_sequence_preview(self):
        """Keep an open custom-point preview in sync while re-picking so the
        newly added points appear immediately instead of only after the user
        clicks preview again."""
        host = self.dialog
        preview = getattr(host, "coord_step_preview", None)
        if preview is None or not getattr(preview, "points", None):
            return
        points = self.sequence_points()
        if not points:
            return
        preview.set_points(points)
        if hasattr(preview, "set_point_labels"):
            preview.set_point_labels([str(i + 1) for i in range(len(points))])

    def start_sequence_pick(self):
        host = self.dialog
        previous = getattr(host, "coord_sequence_picker", None)
        if previous is not None:
            try:
                previous._finish_emitted = True
                previous.close()
            except RuntimeError:
                pass
        host.coord_sequence_picker = MultiPointPickerUI(
            host.append_coord_sequence_point,
            host.on_coord_sequence_pick_finished,
            initial_points=self.sequence_points(),
        )
        picker = host.coord_sequence_picker
        picker.destroyed.connect(
            lambda *_args, _p=picker: setattr(host, "coord_sequence_picker", None)
            if getattr(host, "coord_sequence_picker", None) is _p
            else None
        )

    def on_sequence_pick_finished(self, points):
        host = self.dialog
        host.coord_sequence_picker = None
        self.update_sequence_ui()

    def preview_sequence(self):
        host = self.dialog
        points = self.sequence_points()
        if not points:
            QMessageBox.information(host, "无法预览", "请先添加至少一个自定义点位。")
            return
        self.close_preview()
        self._preview_points_cache = points[:]
        labels = [str(i + 1) for i in range(len(points))]
        batch_mode = host.coord_sequence_batch_combo.currentData() or "one"
        if batch_mode == "all":
            batch_text = "每次进入会点完全部剩余点"
        elif batch_mode == "count":
            count = max(
                1,
                int(
                    parse_float_text(
                        host.coord_sequence_batch_count_edit.text(), 1
                    )
                ),
            )
            batch_text = f"每次进入最多连续点击 {count} 个点"
        else:
            batch_text = "每次进入只点击 1 个点"
        host.coord_step_preview = CoordinateStepPreviewOverlay(
            points,
            {"direction": "自定义点位"},
            title=f"自定义点位预览：{len(points)} 个点",
            detail_text=(
                f"{batch_text}；完成本批后只进行一次分支。"
                "可直接拖动任意点，修改会立即写回下方坐标列表。"
            ),
            auto_close_ms=0,
            point_labels=labels,
            editable_indices=list(range(len(points))),
            point_moved_callback=self.on_sequence_preview_point_moved,
        )
        host.coord_step_preview.point_released_callback = (
            self.on_sequence_preview_point_released
        )
        host.coord_step_preview.destroyed.connect(host.clear_coord_step_preview)

    def on_sequence_preview_point_moved(self, index, x, y):
        cache = getattr(self, "_preview_points_cache", None)
        points = list(cache) if cache is not None else self.sequence_points()
        if not 0 <= int(index) < len(points):
            return None
        points[int(index)] = (int(x), int(y))
        # Keep the cache in sync when it exists; otherwise fall back to
        # parsing the text widget (direct API call without a live preview).
        if cache is not None:
            cache[int(index)] = (int(x), int(y))
        return points

    def on_sequence_preview_point_released(self, index, x, y):
        """Write the final position back to the coordinate text widget."""
        cache = getattr(self, "_preview_points_cache", None)
        points = list(cache) if cache is not None else self.sequence_points()
        if not 0 <= int(index) < len(points):
            return None
        points[int(index)] = (int(x), int(y))
        if cache is not None:
            cache[int(index)] = (int(x), int(y))
        self.dialog.coord_sequence_text.setPlainText(
            serialize_coordinate_sequence(points)
        )
        return points

    def manual_active(self):
        return self.dialog.coord_step_direction_combo.currentText() == "移动到新点位"

    def manual_point_indices(self):
        return sorted(int(idx) for idx in self.dialog.coord_step_manual_points)

    def update_manual_ui(self):
        host = self.dialog
        active = (
            host.coordinate_step_available
            and host.coord_step_chk.isChecked()
            and not host.coord_sequence_chk.isChecked()
            and self.manual_active()
        )
        count = len(host.coord_step_manual_points) if active else 0
        for widget in (
            host.coord_step_manual_info,
            host.coord_step_clear_manual_btn,
            host.coord_step_manual_help,
        ):
            widget.setVisible(active)
        if count:
            nums = "、".join(str(idx + 1) for idx in self.manual_point_indices())
            host.coord_step_manual_info.setText(
                f"已手动修正 {count} 个点：第 {nums} 个"
            )
        else:
            host.coord_step_manual_info.setText("无手动修正点")
        host.coord_step_clear_manual_btn.setEnabled(active and count > 0)

    def current_points(self):
        host = self.dialog
        if host.base_coordinate is None:
            return []
        return build_coord_step_positions(
            host.base_coordinate[0],
            host.base_coordinate[1],
            self.current_options(),
        )

    def refresh_preview_points(self):
        host = self.dialog
        preview = getattr(host, "coord_step_preview", None)
        if not preview:
            return
        points = self.current_points()
        if points:
            preview.set_points(points)
            if hasattr(preview, "set_marked_indices"):
                preview.set_marked_indices(self.manual_point_indices())

    def clear_manual_points(self, silent=False):
        host = self.dialog
        if not host.coord_step_manual_points:
            self.update_manual_ui()
            return
        host.coord_step_manual_points = {}
        self.update_manual_ui()
        self.refresh_preview_points()
        if not silent:
            QToolTip.showText(
                QCursor.pos(), "已清除本步骤的手动修正点", host, QRect(), 1800
            )

    def on_count_changed(self, text):
        host = self.dialog
        if host.coord_step_clearing_due_to_count:
            return
        if host.coord_step_manual_points and str(text) != str(
            host.coord_step_max_steps_initial
        ):
            host.coord_step_clearing_due_to_count = True
            try:
                self.clear_manual_points(silent=True)
                host.coord_step_max_steps_initial = str(text)
            finally:
                host.coord_step_clearing_due_to_count = False

    def current_options(self):
        host = self.dialog
        # FR-20260812-D11-001: 复用 _clamped_int 拒绝 inf/1e309 等非有限值，
        # 防止 int(float('inf')) 抛 OverflowError 从预览路径逃逸到 UI 槽。
        return {
            "every": max(
                1, _clamped_int(host.coord_step_every_edit.text(), 1)
            ),
            "direction": host.coord_step_direction_combo.currentText(),
            "distance": parse_float_text(host.coord_step_distance_edit.text(), 0.0),
            "dx": parse_float_text(host.coord_step_dx_edit.text(), 0.0),
            "dy": parse_float_text(host.coord_step_dy_edit.text(), 0.0),
            "point": host.coord_step_point_edit.text().strip(),
            "max_steps": max(
                0,
                _clamped_int(host.coord_step_max_steps_edit.text(), 0),
            ),
            "max_distance": max(
                0.0,
                parse_float_text(host.coord_step_max_distance_edit.text(), 0.0),
            ),
            "reset_after": max(
                0,
                _clamped_int(host.coord_step_reset_after_edit.text(), 0),
            ),
            "manual_points": dict(host.coord_step_manual_points)
            if self.manual_active()
            else {},
        }

    def start_point_pick(self):
        host = self.dialog
        previous = getattr(host, "coord_step_picker", None)
        if previous is not None:
            try:
                previous._finish_emitted = True
                previous.close()
            except RuntimeError:
                pass
        picker = CoordinatePickerUI("point", host.on_coord_step_point_picked)
        host.coord_step_picker = picker
        picker.destroyed.connect(
            lambda *_args, _p=picker: setattr(host, "coord_step_picker", None)
            if getattr(host, "coord_step_picker", None) is _p
            else None
        )
        picker.show()

    def on_point_picked(self, value):
        host = self.dialog
        host.coord_step_point_edit.setText(value)
        self.update_step_ui()

    def on_preview_point_moved(self, index, x, y):
        host = self.dialog
        try:
            direction = host.coord_step_direction_combo.currentText()
            point_count = len(getattr(host.coord_step_preview, "points", []))
            if index < 0:
                index = point_count + index
            if index == 0:
                value = f"{int(x)},{int(y)}"
                host.base_coordinate = (int(x), int(y))
                if host.base_coordinate_changed:
                    host.base_coordinate_changed(value)
            elif (
                direction == "移动到新点位"
                and point_count > 1
                and index == point_count - 1
            ):
                host.coord_step_point_edit.setText(f"{int(x)},{int(y)}")
            elif direction == "移动到新点位" and 0 < index < max(0, point_count - 1):
                host.coord_step_manual_points[int(index)] = (int(x), int(y))
                self.update_manual_ui()
                if getattr(host, "coord_step_preview", None) and hasattr(
                    host.coord_step_preview, "set_marked_indices"
                ):
                    host.coord_step_preview.set_marked_indices(
                        self.manual_point_indices()
                    )
                # Dragging an intermediate point only overrides that single
                # point (the overlay already moved it visually); the rest of
                # the interpolated path is unchanged, so skip the full
                # build_coord_step_positions recompute. Start/end drags fall
                # through to rebuild the whole path below.
                return None
            options = self.current_options()
            if host.base_coordinate is None:
                return None
            return build_coord_step_positions(
                host.base_coordinate[0], host.base_coordinate[1], options
            )
        except RuntimeError:
            return None

    def show_preview(self):
        host = self.dialog
        if host.base_coordinate is None:
            QMessageBox.information(
                host,
                "无法预览",
                "当前步骤没有可用的起点坐标。请先在步骤参数里直接填写起点坐标，例如 100,200。",
            )
            return
        options = self.current_options()
        if options["direction"] == "移动到新点位" and not parse_coordinate_text(
            options.get("point", "")
        ):
            QMessageBox.information(
                host, "无法预览", "请先填写或选取目标点位，例如 960,540。"
            )
            return
        points = build_coord_step_positions(
            host.base_coordinate[0], host.base_coordinate[1], options
        )
        if len(points) <= 1:
            QMessageBox.information(
                host,
                "无法预览",
                "当前步进设置不会产生新的点击点位，请检查步进方向、距离或目标点位。",
            )
            return
        self.close_preview()
        editable = [0]
        drag_text = "可拖动起点，右键或空白处关闭。"
        if options["direction"] == "移动到新点位":
            editable = list(range(len(points)))
            drag_text = (
                "拖起点/终点会重算整条路径；拖中间点只修正该点并显示星号。"
                "右键或空白处关闭。"
            )
        host.coord_step_preview = CoordinateStepPreviewOverlay(
            points,
            options,
            title=f"坐标步进预览：{len(points)} 个可轮到的点位",
            detail_text=(
                f"实际执行每次只点当前点；再次执行本步骤才移动到下一个点。{drag_text}"
            ),
            editable_indices=editable,
            point_moved_callback=host.on_coord_step_preview_point_moved,
            auto_close_ms=0,
            marked_indices=self.manual_point_indices(),
        )
        host.coord_step_preview.destroyed.connect(host.clear_coord_step_preview)

    def clear_preview(self, *_):
        self.dialog.coord_step_preview = None

    def close_preview(self):
        host = self.dialog
        preview = getattr(host, "coord_step_preview", None)
        if preview:
            try:
                if hasattr(preview, "point_moved_callback"):
                    preview.point_moved_callback = None
            except RuntimeError:
                pass
            safe_close(preview)
            host.coord_step_preview = None
