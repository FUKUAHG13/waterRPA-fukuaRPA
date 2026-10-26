# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Persistent work window for binding profiles to global hotkey slots."""

from __future__ import annotations

import uuid
import weakref

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..profile_hotkeys import MAX_PROFILE_HOTKEY_SLOTS
from ..win32_api import (
    hotkey_signature,
    is_safe_global_hotkey,
    parse_hotkey_text,
)
from .components import (
    NoWheelComboBox,
    enable_work_window_controls,
    fit_text_button,
)
from .input_tools import KeyCaptureDialog
from .lifecycle import schedule_widget_callback


class ProfileHotkeyDialog(QDialog):
    """Edit bounded profile launch slots without switching the visible profile."""

    saved = Signal(object)

    def __init__(self, host, slots):
        super().__init__(None)
        self._host_ref = weakref.ref(host)
        self._slot_ids: list[str] = []
        self._enabled_checks: list[QCheckBox] = []
        self._table_geometry_pending = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        enable_work_window_controls(self)
        self.setWindowModality(Qt.NonModal)
        self.setWindowTitle("多方案快捷启动")
        self.setMinimumSize(880, 430)
        self.resize(1040, 520)

        layout = QVBoxLayout(self)
        note = QLabel(
            "每个槽位绑定一个方案和系统级快捷键。快捷键只启动该方案的快照，"
            "不会切换当前编辑页面；同一时间仍只允许一个脚本或连点器运行。"
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["启用", "方案", "启动热键", "录入", "停止热键（可选）", "录入", "删除"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        for column in (0, 3, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
        layout.addWidget(self.table, 1)

        add_btn = fit_text_button(QPushButton("新增槽位"), 92)
        add_btn.setProperty("variant", "tonal")
        add_btn.clicked.connect(self.add_slot)
        layout.addWidget(add_btn, 0, Qt.AlignLeft)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        for slot in slots:
            self.add_slot(slot)
        self._schedule_table_geometry_refresh()

    @property
    def host(self):
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已关闭")
        return host

    def _profile_combo(self, selected_id: str) -> NoWheelComboBox:
        combo = NoWheelComboBox()
        self._populate_profile_combo(combo, selected_id)
        return combo

    def _populate_profile_combo(
        self, combo: NoWheelComboBox, selected_id: str
    ) -> None:
        combo.clear()
        model = self.host.profile_model
        for name, config in model.profiles.items():
            combo.addItem(name, str(config.get("profile_id", "")))
        index = combo.findData(selected_id)
        if selected_id and index < 0:
            combo.addItem("已删除的方案", selected_id)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def refresh_profiles(self) -> None:
        """Keep open slot editors synchronized with the profile catalog."""

        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            selected_id = str(combo.currentData() or "")
            combo.blockSignals(True)
            self._populate_profile_combo(combo, selected_id)
            combo.blockSignals(False)

    def add_slot(self, slot=None):
        if self.table.rowCount() >= MAX_PROFILE_HOTKEY_SLOTS:
            QMessageBox.information(
                self, "槽位数量已达上限", f"最多允许 {MAX_PROFILE_HOTKEY_SLOTS} 个槽位。"
            )
            return
        data = dict(slot or {})
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._slot_ids.append(str(data.get("slot_id", "") or uuid.uuid4().hex))

        enabled = QCheckBox()
        enabled.setChecked(bool(data.get("enabled", True)))
        self._enabled_checks.append(enabled)
        self.table.setCellWidget(row, 0, self._centered_cell(enabled))
        self.table.setCellWidget(
            row, 1, self._profile_combo(str(data.get("profile_id", "")))
        )

        start_edit = QLineEdit(str(data.get("start_hotkey", "")))
        start_edit.setPlaceholderText("例如 Ctrl+Alt+1")
        start_edit.setReadOnly(True)
        self.table.setCellWidget(row, 2, start_edit)
        start_btn = fit_text_button(QPushButton("录入"), 62)
        start_btn.clicked.connect(lambda _=False, editor=start_edit: self._capture(editor))
        self.table.setCellWidget(row, 3, start_btn)

        stop_edit = QLineEdit(str(data.get("stop_hotkey", "")))
        stop_edit.setPlaceholderText("可留空，使用全局停止热键")
        stop_edit.setReadOnly(True)
        self.table.setCellWidget(row, 4, stop_edit)
        stop_btn = fit_text_button(QPushButton("录入"), 62)
        stop_btn.clicked.connect(lambda _=False, editor=stop_edit: self._capture(editor))
        self.table.setCellWidget(row, 5, stop_btn)

        remove_btn = fit_text_button(QPushButton("删除"), 62)
        remove_btn.setProperty("variant", "dangerGhost")
        remove_btn.clicked.connect(
            lambda _=False, button=remove_btn: self._remove_button_row(button)
        )
        self.table.setCellWidget(row, 6, remove_btn)
        self._schedule_table_geometry_refresh()

    @staticmethod
    def _centered_cell(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _schedule_table_geometry_refresh(self):
        if self._table_geometry_pending:
            return
        self._table_geometry_pending = True
        # FR-20260809-0906: 对话框销毁后延迟回调不得触碰已删除控件。
        schedule_widget_callback(self, 0, lambda w: w._refresh_table_geometry())

    def _refresh_table_geometry(self):
        self._table_geometry_pending = False
        header = self.table.horizontalHeader()
        for column in (0, 3, 5, 6):
            item = self.table.horizontalHeaderItem(column)
            widths = [item.sizeHint().width() if item is not None else 0]
            for row in range(self.table.rowCount()):
                widget = self.table.cellWidget(row, column)
                if widget is not None:
                    widths.append(widget.sizeHint().width())
            header.resizeSection(column, max(widths, default=0) + 16)
        minimum_height = self.table.verticalHeader().minimumSectionSize()
        for row in range(self.table.rowCount()):
            heights = [minimum_height]
            for column in range(self.table.columnCount()):
                widget = self.table.cellWidget(row, column)
                if widget is not None:
                    heights.append(widget.sizeHint().height() + 8)
            self.table.setRowHeight(row, max(heights))

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_table_geometry_refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.FontChange,
            QEvent.StyleChange,
            QEvent.ApplicationFontChange,
        ):
            self._schedule_table_geometry_refresh()

    def _capture(self, editor: QLineEdit):
        dialog = KeyCaptureDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        text = str(
            getattr(dialog, "result_text", "")
            or getattr(dialog, "captured_text", "")
        ).strip()
        parsed = parse_hotkey_text(text)
        if not parsed or not is_safe_global_hotkey(parsed):
            QMessageBox.warning(
                self,
                "快捷键不适合全局启停",
                "请使用 F1-F12 等功能键，或带 Ctrl/Alt/Shift/Win 的组合键。",
            )
            return
        editor.setText(str(parsed["display"]))

    def _remove_button_row(self, button: QPushButton):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 6) is button:
                self.table.removeRow(row)
                self._slot_ids.pop(row)
                self._enabled_checks.pop(row)
                self._schedule_table_geometry_refresh()
                return

    def _save(self):
        slots = []
        used: dict[tuple[int, int], str] = {}
        reserved = self.host.hotkey_controller.reserved_hotkey_signatures(
            include_profile=False
        )
        for row in range(self.table.rowCount()):
            enabled = self._enabled_checks[row].isChecked()
            profile_combo = self.table.cellWidget(row, 1)
            profile_id = str(profile_combo.currentData() or "")
            start_text = self.table.cellWidget(row, 2).text().strip()
            stop_text = self.table.cellWidget(row, 4).text().strip()
            slot = {
                "slot_id": self._slot_ids[row],
                "enabled": enabled,
                "profile_id": profile_id,
                "start_hotkey": start_text,
                "stop_hotkey": stop_text,
            }
            slots.append(slot)
            if not enabled:
                continue
            if not self.host.profile_model.find_by_id(profile_id):
                QMessageBox.warning(self, "方案不存在", f"第 {row + 1} 个槽位绑定的方案已被删除。")
                return
            if not start_text:
                QMessageBox.warning(self, "缺少启动热键", f"第 {row + 1} 个槽位必须设置启动热键。")
                return
            for label, text in (("启动", start_text), ("停止", stop_text)):
                if not text:
                    continue
                parsed = parse_hotkey_text(text)
                if not parsed or not is_safe_global_hotkey(parsed):
                    QMessageBox.warning(self, "快捷键无效", f"第 {row + 1} 个槽位的{label}热键无效。")
                    return
                signature = hotkey_signature(parsed)
                owner = used.get(signature) or reserved.get(signature)
                if owner:
                    QMessageBox.warning(
                        self,
                        "快捷键冲突",
                        f"第 {row + 1} 个槽位的{label}热键与“{owner}”冲突。",
                    )
                    return
                used[signature] = f"第 {row + 1} 个槽位{label}热键"
        self.saved.emit(slots)
        self.close()
