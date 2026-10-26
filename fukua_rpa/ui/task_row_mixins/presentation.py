# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""TaskRow 的分组/备注/标记呈现 Mixin（God Class 拆分）。

从 ``TaskRow`` 提取的呈现方法组：分组头与层级呈现、步骤备注编辑与显示、
编辑器元数据属性、停/注/隐/断等标记刷新。控件与数据仍归主类所有，
本 Mixin 不持有任何状态。
"""

from PySide6.QtWidgets import QGraphicsOpacityEffect, QStyle

from ...workflow_metadata import normalize_editor_metadata, normalize_step_note
from ..lifecycle import is_runtime_editing_locked


class TaskRowPresentationMixin:
    """分组头、备注与步骤标记呈现（8 方法）。"""

    def set_group_presentation(self, presentation):
        presentation = presentation if isinstance(presentation, dict) else {}
        headers = presentation.get("headers", [])
        for index, frame in enumerate(getattr(self, "group_header_frames", ())):
            if index >= len(headers):
                frame.hide()
                continue
            header = headers[index]
            depth = max(1, min(3, int(header.get("depth", index + 1))))
            frame.setProperty("groupDepth", depth)
            frame.layout().setContentsMargins(8 + (depth - 1) * 14, 3, 8, 3)
            self.group_header_labels[index].setText(str(header.get("name", "未命名分组")))
            count = max(1, int(header.get("count", 1)))
            self.group_header_counts[index].setText(f"{count} 步")
            collapsed = bool(header.get("collapsed", False))
            button = self.group_toggle_buttons[index]
            button.setProperty("groupId", str(header.get("id", "")))
            button.setIcon(
                self.style().standardIcon(
                    QStyle.SP_ArrowRight if collapsed else QStyle.SP_ArrowDown
                )
            )
            button.setToolTip("展开分组" if collapsed else "折叠分组")
            frame.show()
        depth = max(0, min(3, int(presentation.get("depth", 0))))
        self.controls_widget.setVisible(
            not bool(presentation.get("controls_hidden", False))
        )
        self.layout.setContentsMargins(8 + depth * 10, 5, 7, 5)
        for name, value in {
            "groupDepth": depth,
            "groupStart": bool(presentation.get("group_start", False)),
            "groupEnd": bool(presentation.get("group_end", False)),
        }.items():
            self.setProperty(name, value)
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()
        if self.parent_item is not None:
            # Group refreshes intentionally rewrite source margins above.  If
            # this row lives on a screen with a different DPI, reapply the
            # window's visual density after that rewrite so margins do not jump
            # back to primary-screen spacing.
            scale_row = getattr(self.window(), "apply_ui_scale_to_widget", None)
            if callable(scale_row):
                scale_row(self)
            self.parent_item.setSizeHint(self.sizeHint())

    def toggle_group_header(self, index):
        buttons = getattr(self, "group_toggle_buttons", ())
        if not 0 <= int(index) < len(buttons):
            return
        group_id = str(buttons[int(index)].property("groupId") or "")
        main_window = self.window()
        toggle = getattr(getattr(main_window, "workflow_editor", None), "toggle_group_collapsed", None)
        if group_id and callable(toggle):
            toggle(group_id)

    def group_id_at_position(self, position):
        for frame, button in zip(
            getattr(self, "group_header_frames", ()),
            getattr(self, "group_toggle_buttons", ()), strict=False,
        ):
            if frame.isVisible() and frame.geometry().contains(position):
                return str(button.property("groupId") or "")
        return ""

    def apply_editor_metadata(self):
        metadata = normalize_editor_metadata(self.custom_data)
        self.custom_data.update(metadata)
        note = metadata["step_note"]
        self.note_display.setText(note)
        self.note_display.setToolTip(
            note or "暂无步骤备注；可在步骤设置或右键菜单中修改"
        )
        self.setToolTip(note)
        self.index_label.setToolTip(note or "点击步骤序号可选中；按 Ctrl 或 Shift 可多选")
        self.setAccessibleDescription(note)
        properties = {
            "markerColor": metadata["marker_color"],
            "editorHidden": metadata["editor_hidden"],
        }
        changed = False
        for name, value in properties.items():
            if self.property(name) != value:
                self.setProperty(name, value)
                changed = True
        if changed:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def update_markers(self):
        self.apply_editor_metadata()
        self.update_manual_marker()
        disabled = self.is_step_disabled()
        self.disabled_mark_label.setVisible(disabled)
        if bool(self.property("stepDisabled")) != disabled:
            self.setProperty("stepDisabled", disabled)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        has_breakpoint = self.has_breakpoint()
        condition = str(self.custom_data.get("debug_condition", "") or "").strip()
        self.breakpoint_mark_label.setToolTip(
            f"条件断点：{condition}"
            if has_breakpoint and condition
            else "调试断点：运行到本步骤时会在执行前暂停。按 F8 可切换。"
        )
        self.breakpoint_mark_label.setVisible(
            has_breakpoint and self.settings_mode == "advanced"
        )

    def set_hidden_reveal_faded(self, enabled):
        enabled = bool(enabled)
        if enabled and self._hidden_reveal_effect is None:
            self._hidden_reveal_effect = QGraphicsOpacityEffect(self)
            self._hidden_reveal_effect.setOpacity(0.42)
            self._hidden_reveal_effect.setEnabled(False)
            self.setGraphicsEffect(self._hidden_reveal_effect)
        if self._hidden_reveal_effect is None:
            return
        if self._hidden_reveal_effect.isEnabled() == enabled:
            return
        self._hidden_reveal_effect.setEnabled(enabled)
        self.update()

    def on_note_edited(self):
        """Commit an inline note edit back into the step data."""
        if self.note_display is None:
            return
        # INVARIANT: 运行时编辑锁启用期间不得提交任何步骤编辑，备注编辑同样受此约束。
        if is_runtime_editing_locked(self.window()):
            return
        new_note = normalize_step_note(self.note_display.text())
        if new_note == normalize_step_note(self.custom_data.get("step_note", "")):
            return
        self.commit_external_edit(
            "备注",
            lambda: self.custom_data.__setitem__("step_note", new_note),
        )
        self.note_display.setText(new_note)

    def set_note_display_visible(self, enabled):
        self._show_note_display = bool(enabled)
        self.note_display.setVisible(self._show_note_display)
        self._sync_note_column_width()
        self.updateGeometry()
        if self.parent_item is not None:
            self.parent_item.setSizeHint(self.sizeHint())
