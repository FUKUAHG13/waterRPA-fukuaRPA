# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Editor widget for ordered image-to-action rules."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..constants import MAX_MULTI_IMAGE_ACTION_RULES
from ..multi_image_actions import (
    MULTI_IMAGE_CHILD_ACTIONS,
    MULTI_IMAGE_POINT_ACTIONS,
    multi_image_action_value_hint,
    normalize_multi_image_rules,
)
from .components import NoWheelComboBox, fit_text_button
from .lifecycle import safe_close
from .region_window_manager import region_window_manager
from .template_notice import show_transparent_template_warning_once


class MultiImageActionEditor(QWidget):
    """Keep rule order explicit: the first matching row owns the action."""

    def __init__(self, rules, *, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.region_window = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self.add_btn = QPushButton("添加规则")
        self.remove_btn = QPushButton("删除")
        self.up_btn = QPushButton("上移")
        self.down_btn = QPushButton("下移")
        self.region_btn = QPushButton("框选区域")
        for button, width in (
            (self.add_btn, 88),
            (self.remove_btn, 68),
            (self.up_btn, 68),
            (self.down_btn, 68),
            (self.region_btn, 88),
        ):
            fit_text_button(button, width)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["图片（按行顺序匹配）", "命中后动作", "动作参数", "识别区域", "相似度"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(250)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        self.add_btn.clicked.connect(self.add_rule)
        self.remove_btn.clicked.connect(self.remove_selected_rule)
        self.up_btn.clicked.connect(lambda: self.move_selected_rule(-1))
        self.down_btn.clicked.connect(lambda: self.move_selected_rule(1))
        self.region_btn.clicked.connect(self.pick_selected_region)
        self.load_rules(rules)

    def _image_editor(self, path=""):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        edit = QLineEdit(str(path or ""))
        edit.setPlaceholderText("选择模板图片")
        button = QPushButton("图")
        button.setFixedWidth(30)
        button.setProperty("iconOnly", True)
        button.setToolTip("选择此规则要识别的模板图片")
        button.clicked.connect(lambda: self.select_rule_image(edit))
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        container.image_edit = edit
        return container

    def _action_editor(self, code="left_click"):
        combo = NoWheelComboBox()
        for action in MULTI_IMAGE_CHILD_ACTIONS:
            combo.addItem(action.label, action.code)
        index = combo.findData(str(code or "left_click"))
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            # FR-20260812-D11-002: 未知子动作码不得落位索引 0（left_click）。
            # 保留原值作为占位项，供校验层报告"命中动作无效"，避免导入/未来
            # 格式步骤被静默改写为可执行的左键单击。
            combo.addItem(f"未知动作（{code}）", str(code))
            combo.setCurrentIndex(combo.count() - 1)
        return combo

    def load_rules(self, rules):
        self.table.setRowCount(0)
        for rule in normalize_multi_image_rules(rules):
            self._append_rule(rule)
        if self.table.rowCount():
            self.table.selectRow(0)

    def _append_rule(self, rule=None):
        rule = dict(rule or {})
        row = self.table.rowCount()
        self.table.insertRow(row)
        image_editor = self._image_editor(rule.get("image", ""))
        action_editor = self._action_editor(rule.get("action", "left_click"))
        value_edit = QLineEdit(str(rule.get("value", "") or ""))
        region_edit = QLineEdit(str(rule.get("region", "") or ""))
        region_edit.setPlaceholderText("留空沿用本步/全局区域")
        confidence_edit = QLineEdit(str(rule.get("confidence", "") or ""))
        confidence_edit.setPlaceholderText("沿用")
        confidence_edit.setFixedWidth(68)
        self.table.setCellWidget(row, 0, image_editor)
        self.table.setCellWidget(row, 1, action_editor)
        self.table.setCellWidget(row, 2, value_edit)
        self.table.setCellWidget(row, 3, region_edit)
        self.table.setCellWidget(row, 4, confidence_edit)
        action_editor.currentIndexChanged.connect(
            lambda _index, combo=action_editor, edit=value_edit: self._update_value_editor(
                combo, edit
            )
        )
        self._update_value_editor(action_editor, value_edit)
        self.table.setRowHeight(row, 38)
        return row

    def _update_value_editor(self, combo, edit):
        code = str(combo.currentData() or "left_click")
        point_action = code in MULTI_IMAGE_POINT_ACTIONS
        edit.setEnabled(not point_action)
        edit.setPlaceholderText(
            "命中位置自动作为参数" if point_action else multi_image_action_value_hint(code)
        )

    def add_rule(self):
        if self.table.rowCount() >= MAX_MULTI_IMAGE_ACTION_RULES:
            QMessageBox.information(
                self,
                "规则数量已达上限",
                f"每个多图动作步骤最多可添加 {MAX_MULTI_IMAGE_ACTION_RULES} 条规则。",
            )
            return
        row = self._append_rule()
        self.table.selectRow(row)
        image_editor = self.table.cellWidget(row, 0)
        image_editor.image_edit.setFocus(Qt.OtherFocusReason)

    def selected_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def remove_selected_rule(self):
        row = self.selected_row()
        if row < 0:
            return
        self.table.removeRow(row)
        if self.table.rowCount():
            self.table.selectRow(min(row, self.table.rowCount() - 1))

    def move_selected_rule(self, offset):
        row = self.selected_row()
        target = row + int(offset)
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        rules = self.rules()
        rules[row], rules[target] = rules[target], rules[row]
        # Full table rebuild on move is acceptable: rule count is typically < 10.
        # Partial update would add complexity for negligible gain.
        self.load_rules(rules)
        self.table.selectRow(target)

    def select_rule_image(self, edit):
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择规则模板图片",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp)",
        )
        if not path:
            return
        edit.setText(path)
        show_transparent_template_warning_once(self, self.settings, path)

    def pick_selected_region(self):
        row = self.selected_row()
        if row < 0:
            QMessageBox.information(self, "请先选择规则", "请先选中要设置识别区域的规则。")
            return
        # RegionWindowManager 确保在创建新窗口前关闭旧的（destroyed 清理引用）。
        region_window_manager.close(self, "multi_image_region")
        window = region_window_manager.open(
            host=self,
            key="multi_image_region",
            purpose="recognition",
            on_selected=lambda rect, selected=row: self._apply_picked_region(
                selected, rect
            ),
        )
        self.region_window = window
        window.destroyed.connect(
            lambda *_args: setattr(self, "region_window", None)
        )

    def _apply_picked_region(self, row, rect):
        if not 0 <= row < self.table.rowCount():
            return
        editor = self.table.cellWidget(row, 3)
        if editor is not None:
            editor.setText(",".join(str(int(value)) for value in rect))

    def rules(self):
        rules = []
        for row in range(self.table.rowCount()):
            image_editor = self.table.cellWidget(row, 0)
            action_editor = self.table.cellWidget(row, 1)
            value_editor = self.table.cellWidget(row, 2)
            region_editor = self.table.cellWidget(row, 3)
            confidence_editor = self.table.cellWidget(row, 4)
            action_code = str(action_editor.currentData() or "left_click")
            # FR-20260812-D11-002: 未知子动作码原样输出，由模型层
            # normalize_multi_image_action_code 保留、校验层报告
            # "命中动作无效"，不得静默改写为 left_click。
            rules.append(
                {
                    "image": image_editor.image_edit.text().strip(),
                    "action": action_code,
                    "value": value_editor.text(),
                    "region": region_editor.text().strip(),
                    "confidence": confidence_editor.text().strip(),
                }
            )
        return normalize_multi_image_rules(rules)

    def closeEvent(self, event):
        self.close_tools()
        super().closeEvent(event)

    def close_tools(self):
        # Close whatever widget is referenced directly so it can never leak
        # when it was not opened through region_window_manager (e.g. a picker
        # attached by tests or a future refactor). The manager close below is
        # idempotent when the same window is manager-owned.
        safe_close(self.region_window)
        region_window_manager.close(self, "multi_image_region")
        self.region_window = None
