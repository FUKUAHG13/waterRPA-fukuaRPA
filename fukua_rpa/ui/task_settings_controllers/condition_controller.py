"""Until-condition interaction coordination for per-step settings."""

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ...task_model import format_region_text, parse_region_text
from ..region_window_manager import region_window_manager
from ..template_notice import show_transparent_template_warning_once


class UntilConditionController:
    def __init__(self, dialog):
        self.dialog = dialog

    def update_ui(self, _=None):
        host = self.dialog
        active = host.is_until_condition_task()
        host.until_group.setEnabled(active)
        if not active:
            return
        enabled_count = 0
        for widgets in host.until_condition_widgets.values():
            checked = widgets["enabled"].isChecked()
            enabled_count += 1 if checked else 0
            mode = widgets["mode"].currentText()
            is_overlay = mode == "覆盖匹配"
            needs_image = mode in ["图片出现", "图片消失", "区域变成指定图片"]
            needs_region = mode in ["区域发生变化", "区域变成指定图片", "覆盖匹配", "区域不变"]
            widgets["mode"].setEnabled(checked)
            widgets["image"].setEnabled(checked and needs_image)
            widgets["image_btn"].setEnabled(checked and needs_image)
            widgets["region"].setEnabled(checked)
            widgets["region_btn"].setEnabled(checked)
            widgets["conf"].setEnabled(checked and mode in ["图片出现", "图片消失"])
            widgets["diff"].setEnabled(checked and mode in ("区域发生变化", "区域不变"))
            widgets["similarity"].setEnabled(checked and mode == "区域变成指定图片")
            widgets["stable"].setEnabled(checked and mode == "区域不变")
            widgets["region"].setPlaceholderText(
                "必填区域 x,y,w,h" if needs_region else "区域 x,y,w,h，可空"
            )
            # DESIGN: 子区域变化检测专属行仅在 mode 为"区域发生变化"且条件启用时显示。
            sub_region_row = widgets.get("sub_region_row")
            if sub_region_row is not None:
                show_sub_region = checked and mode == "区域发生变化"
                sub_region_row.setVisible(show_sub_region)
                if show_sub_region:
                    widgets["sub_region"].setEnabled(True)
                    widgets["sub_region_btn"].setEnabled(True)
            # DESIGN: 覆盖匹配专属行仅在 mode 为"覆盖匹配"且条件启用时显示。
            overlay_row = widgets.get("overlay_row")
            if overlay_row is not None:
                overlay_row.setVisible(checked and is_overlay)
                if checked and is_overlay:
                    widgets["overlay_image"].setEnabled(True)
                    widgets["overlay_image_btn"].setEnabled(True)
                    widgets["overlay_rect"].setEnabled(True)
                    widgets["overlay_rect_btn"].setEnabled(True)
                    widgets["overlay_scale"].setEnabled(True)
                    widgets["overlay_threshold"].setEnabled(True)
        host.until_logic_combo.setEnabled(enabled_count > 1)

    def select_image(self, cond_idx):
        host = self.dialog
        widgets = host.until_condition_widgets.get(cond_idx)
        if not widgets:
            return
        path, _ = QFileDialog.getOpenFileName(
            host, "选择条件图片", filter="Images (*.png *.jpg *.bmp)"
        )
        if path:
            widgets["image"].setText(path)
            show_transparent_template_warning_once(
                host, host.dialog_settings, path
            )

    def select_overlay_image(self, cond_idx):
        host = self.dialog
        widgets = host.until_condition_widgets.get(cond_idx)
        if not widgets:
            return
        path, _ = QFileDialog.getOpenFileName(
            host, "选择覆盖匹配小图", filter="Images (*.png *.jpg *.bmp)"
        )
        if path:
            widgets["overlay_image"].setText(path)

    def start_region_pick(self, cond_idx):
        host = self.dialog
        region_window_manager.close(host, ("until", cond_idx))
        win = region_window_manager.open(
            host=host,
            key=("until", cond_idx),
            purpose="condition",
            on_selected=lambda rect, i=cond_idx: host.on_until_region_selected(i, rect),
        )
        host.until_region_windows[cond_idx] = win
        win.destroyed.connect(
            lambda *_args, i=cond_idx: host.until_region_windows.pop(i, None)
        )

    def start_overlay_rect_pick(self, cond_idx):
        """框选覆盖匹配的子区域位置（相对于大区域）。"""
        host = self.dialog
        region_window_manager.close(host, ("until", cond_idx))
        win = region_window_manager.open(
            host=host,
            key=("until", cond_idx),
            purpose="condition",
            on_selected=lambda rect, i=cond_idx: self._on_overlay_rect_selected(i, rect),
        )
        host.until_region_windows[cond_idx] = win
        win.destroyed.connect(
            lambda *_args, i=cond_idx: host.until_region_windows.pop(i, None)
        )

    def _on_overlay_rect_selected(self, cond_idx, rect):
        widgets = self.dialog.until_condition_widgets.get(cond_idx)
        if widgets:
            widgets["overlay_rect"].setText(format_region_text(rect))

    def on_region_selected(self, cond_idx, rect):
        widgets = self.dialog.until_condition_widgets.get(cond_idx)
        if widgets:
            widgets["region"].setText(format_region_text(rect))

    def start_sub_region_pick(self, cond_idx):
        """框选“区域发生变化”条件的子区域（限制在大区域内，坐标相对大区域）。"""
        host = self.dialog
        widgets = host.until_condition_widgets.get(cond_idx)
        if not widgets:
            return
        region = parse_region_text(widgets["region"].text())
        if not region:
            QMessageBox.information(
                host,
                "先框选大区域",
                "请先用“区”按钮框选大区域，再在其中框选只检测变化的子区域。",
            )
            return
        region_window_manager.close(host, ("until", cond_idx))
        # DESIGN: restrict_rect 把框选限制在大区域范围内，避免用户选到大区域之外
        # 的屏幕区域；选完后在 _on_sub_region_selected 中换算成相对大区域的偏移。
        win = region_window_manager.open(
            host=host,
            key=("until", cond_idx),
            purpose="condition",
            restrict_rect=region,
            on_selected=lambda rect, i=cond_idx: self._on_sub_region_selected(i, rect),
        )
        host.until_region_windows[cond_idx] = win
        win.destroyed.connect(
            lambda *_args, i=cond_idx: host.until_region_windows.pop(i, None)
        )

    def _on_sub_region_selected(self, cond_idx, rect):
        host = self.dialog
        widgets = host.until_condition_widgets.get(cond_idx)
        if not widgets:
            return
        region = parse_region_text(widgets["region"].text())
        if not region:
            return
        rx, ry, rw, rh = (int(value) for value in rect)
        bx, by, bw, bh = (int(value) for value in region)
        # 兜底裁剪：把选区收敛到大区域内，再换算成相对大区域左上角的偏移。
        left = max(rx, bx)
        top = max(ry, by)
        right = min(rx + rw, bx + bw)
        bottom = min(ry + rh, by + bh)
        if right <= left or bottom <= top:
            return
        relative = (left - bx, top - by, right - left, bottom - top)
        widgets["sub_region"].setText(format_region_text(relative))
