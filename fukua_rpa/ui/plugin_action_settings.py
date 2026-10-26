# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Manifest-driven settings dialog for a generic plugin-action step.

Opened from the step settings window when a step of type ``TASK_TYPE_PLUGIN_ACTION``
is being edited. The dialog is driven entirely by the selected plugin's manifest
(design §5.4): it lets the user pick an installed, enabled, compatible plugin, an
action declared by that plugin, and fills a parameter form generated from the
action's declared ``input_schema``. It never accepts arbitrary Qt widgets or
Python callbacks from the plugin (design §5.4, last paragraph).

On accept it writes the canonical step fields back into the step dict:
``plugin_id``, ``plugin_action_id``, ``plugin_schema_version``, ``plugin_config``,
and a human-readable ``value`` label. The caller is responsible for persisting the
step dict (e.g. via the task-settings dialog's normal save path).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
)

from ..constants import TASK_TYPE_PLUGIN_ACTION
from ..json_safety import loads_bounded_json
from ..plugin_registry import PluginRegistry, PluginStatus
from .components import FloatingSettingsDialog, fit_text_button

# Bound for one JSON-typed plugin parameter typed in the editor. Plugin action
# params are small scalars/containers; this mirrors the "bounded JSON input"
# contract (ARCHITECTURE.md #61, design §JSON 解析边界) instead of an unbounded
# json.loads.
_MAX_JSON_PARAM_BYTES = 256 * 1024


def _plugin_display(rec) -> str:
    return f"{rec.name}（v{rec.version}）" if rec.name else rec.plugin_id


class PluginActionSettings(FloatingSettingsDialog):
    """Edit the plugin/action/config of a plugin-action step."""

    def __init__(
        self,
        settings,
        task: dict,
        registry: PluginRegistry | None,
        on_accept: Callable[[dict], None],
        parent=None,
    ):
        super().__init__(settings, "plugin_action_geometry", "插件动作设置", (520, 560))
        self._task = task
        self._registry = registry
        self._on_accept = on_accept

        self._plugin_combo = QComboBox()
        self._action_combo = QComboBox()
        self._param_area = QFrame(self)
        self._param_layout = QFormLayout(self._param_area)
        self._param_layout.setContentsMargins(0, 0, 0, 0)
        self._param_widgets: list[tuple] = []  # (key, widget)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setProperty("role", "muted")

        self._build_ui()
        self._populate_plugins()
        self._restore_selection()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addWidget(QLabel("插件"))
        root.addWidget(self._plugin_combo)
        self._plugin_combo.currentIndexChanged.connect(self._on_plugin_changed)

        root.addWidget(QLabel("动作"))
        root.addWidget(self._action_combo)
        self._action_combo.currentIndexChanged.connect(self._on_action_changed)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._param_area)
        root.addWidget(QLabel("参数"))
        root.addWidget(scroll, 1)

        root.addWidget(self._note)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = fit_text_button(QPushButton("确定"))
        cancel_btn = fit_text_button(QPushButton("取消"))
        ok_btn.clicked.connect(self._on_accept_clicked)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    def _populate_plugins(self):
        self._plugin_combo.clear()
        self._plugin_combo.addItem("（未选择插件）", "")
        if self._registry is None:
            return
        for rec in self._registry.list_plugins():
            if rec.status != PluginStatus.ENABLED:
                continue
            self._plugin_combo.addItem(_plugin_display(rec), rec.plugin_id)

    def _restore_selection(self):
        pid = self._task.get("plugin_id", "")
        idx = self._plugin_combo.findData(pid)
        if idx >= 0:
            self._plugin_combo.setCurrentIndex(idx)
        else:
            self._on_plugin_changed()
        aid = self._task.get("plugin_action_id", "")
        if aid:
            aidx = self._action_combo.findData(aid)
            if aidx >= 0:
                self._action_combo.setCurrentIndex(aidx)

    def _on_plugin_changed(self):
        self._action_combo.clear()
        self._action_combo.addItem("（未选择动作）", "")
        plugin_id = self._plugin_combo.currentData()
        if not plugin_id or self._registry is None:
            self._clear_params()
            return
        manifest = self._registry.get_manifest(plugin_id)
        if manifest is None:
            self._clear_params()
            return
        for action in manifest.actions:
            self._action_combo.addItem(f"{action.name}（{action.id}）", action.id)
        aid = self._task.get("plugin_action_id", "")
        aidx = self._action_combo.findData(aid)
        if aidx >= 0:
            self._action_combo.setCurrentIndex(aidx)
        else:
            self._on_action_changed()

    def _on_action_changed(self):
        self._clear_params()
        plugin_id = self._plugin_combo.currentData()
        action_id = self._action_combo.currentData()
        if not plugin_id or not action_id or self._registry is None:
            return
        action = self._registry.get_action(plugin_id, action_id)
        if action is None:
            return
        schema = getattr(action, "input_schema", {}) or {}
        # FR-RISK-20261005-603: plugin_config is loaded from disk (or hand-edited)
        # and must be a mapping before we index it; a non-dict truthy value (e.g.
        # an int) would raise TypeError and crash the dialog.
        raw_current = self._task.get("plugin_config")
        current = raw_current if isinstance(raw_current, dict) else {}
        self._build_params(schema, current)
        desc = getattr(action, "description", "")
        if desc:
            self._note.setText(desc)

    def _clear_params(self):
        while self._param_layout.rowCount():
            self._param_layout.removeRow(0)
        self._param_widgets = []

    def _build_params(self, schema: dict, current: dict):
        self._clear_params()
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(properties, dict) or not properties:
            self._param_layout.addRow(QLabel("（该动作没有可配置参数）"))
            return
        required_raw = schema.get("required", []) or []
        required = set(required_raw) if isinstance(required_raw, (list, tuple)) else set()
        for key, prop in properties.items():
            if not isinstance(prop, dict):
                continue
            ptype = prop.get("type", "string")
            title = prop.get("title", key)
            # FR-20261003-604: 已有 plugin_config 的当前值优先；仅当键缺失或为
            # None 时才采用清单声明的 default，避免界面默认态即违反 schema。
            seed = (
                current[key]
                if key in current and current[key] is not None
                else prop.get("default")
            )
            widget = self._make_widget(ptype, seed, prop)
            self._param_widgets.append((key, widget))
            label = QLabel(title + (" *" if key in required else ""))
            if prop.get("description"):
                label.setToolTip(prop["description"])
            self._param_layout.addRow(label, widget)

    @staticmethod
    def _json_kind(ptype: str) -> bool:
        """FR-20261003-603: array/object 需要 JSON 文本编辑器与反序列化。"""
        return ptype in ("array", "object")

    def _make_widget(self, ptype: str, value, prop: dict | None = None):
        prop = prop if isinstance(prop, dict) else {}
        # FR-20261003-603/604: enum 参数使用下拉框，保证只能选出合法项；若历史值
        # 不在候选内，则作为占位项保留（不静默改写用户已保存的值）。
        enum_values = prop.get("enum")
        if isinstance(enum_values, (list, tuple)) and enum_values:
            w = QComboBox()
            matched = False
            for candidate in enum_values:
                w.addItem(str(candidate), candidate)
                if value is not None and candidate == value:
                    matched = True
            if value is not None and not matched:
                w.addItem(f"{value}（当前值，不在允许范围）", value)
                w.setCurrentIndex(w.count() - 1)
            elif matched:
                idx = w.findData(value)
                if idx >= 0:
                    w.setCurrentIndex(idx)
            return w
        if ptype in ("integer", "int"):
            w = QSpinBox()
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            low = int(minimum) if isinstance(minimum, (int, float)) else -(2 ** 31)
            high = int(maximum) if isinstance(maximum, (int, float)) else 2 ** 31 - 1
            if low > high:
                low, high = high, low
            w.setRange(low, high)
            try:
                number = int(value if value is not None else 0)
            except (TypeError, ValueError):
                w.setValue(low)
                return w
            # FR-20261006-011: QSpinBox.setValue 把越界值**静默钳位**到
            # minimum/maximum，``_collect_config`` 随后读回该钳位值，用户已保存的
            # 999 会在「打开设置→保存」后变成 10——原值永久丢失且无任何提示。
            # 这与本函数 enum 分支「不在候选内则作为占位项保留、不静默改写」的
            # 口径矛盾。越界时**不写入数值控件**，而是禁用该控件并把原值记在
            # ``plugin_out_of_range_value`` 属性上；``_collect_config`` 见到该
            # 属性时原样回写用户已保存的值，由运行前校验按超范围报错。
            if number < low or number > high:
                w.setValue(low)
                w.setEnabled(False)
                w.setProperty("plugin_out_of_range_value", number)
                w.setToolTip(
                    f"已保存的值 {number} 超出该参数允许范围 {low}~{high}，"
                    "已保留原值、未写回，也不会被静默改写。"
                )
                return w
            w.setValue(number)
            return w
        if ptype in ("number", "float"):
            w = QDoubleSpinBox()
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            low = float(minimum) if isinstance(minimum, (int, float)) else -(1e12)
            high = float(maximum) if isinstance(maximum, (int, float)) else 1e12
            if low > high:
                low, high = high, low
            w.setRange(low, high)
            try:
                number = float(value if value is not None else 0.0)
            except (TypeError, ValueError):
                w.setValue(low)
                return w
            # FR-20261006-011: 与整数分支同因——QDoubleSpinBox.setValue 同样会
            # 静默钳位，处理方式一致。
            if number < low or number > high:
                w.setValue(low)
                w.setEnabled(False)
                w.setProperty("plugin_out_of_range_value", value)
                w.setToolTip(
                    f"已保存的值 {number} 超出该参数允许范围 {low}~{high}，"
                    "已保留原值、未写回，也不会被静默改写。"
                )
                return w
            w.setValue(number)
            return w
        if ptype == "boolean":
            w = QCheckBox()
            # FR-20260809-0901: bool("false")/bool("0") 均求值为 True，字符串形态的
            # boolean 参数（旧数据/外部导入）会被误勾选并在保存时改写为 True。
            # 显式解析常见真值；其余回退 False。
            w.setChecked(
                value in (True, 1, "1", "true", "True", "TRUE", "on", "yes")
            )
            return w
        if self._json_kind(ptype):
            # FR-20261003-603: array/object 以 JSON 文本编辑；留空表示「未提供」
            # （在 _collect_config 中直接省略该键），合法 JSON 反序列化为 list/dict，
            # 非法 JSON 在保存前给出明确提示而不是静默转换。
            w = QLineEdit()
            if value is not None:
                try:
                    w.setText(json.dumps(value, ensure_ascii=False))
                except (TypeError, ValueError):
                    w.setText("")
            w.setPlaceholderText("JSON 数组" if ptype == "array" else "JSON 对象")
            # 标记该控件需要 JSON 反序列化；_collect_config 据此解析。
            w.setProperty("plugin_json_kind", True)
            return w
        # default: string / text
        w = QLineEdit()
        w.setText("" if value is None else str(value))
        return w

    def _collect_config(self) -> dict:
        """收集参数控件值。

        Raises:
            ValueError: ``array``/``object`` 参数填写了非法 JSON（FR-20261003-603）。
        """
        out: dict[str, Any] = {}
        for key, widget in self._param_widgets:
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                # FR-20261006-011: 控件被禁用说明已保存值越界（见 ``_make_widget``）。
                # 此时必须回写**原值**，而不是控件被钳位后的数值，否则用户原值
                # 会在「打开设置→保存」时被静默改写。运行前校验会按超范围报错。
                if widget.property("plugin_out_of_range_value") is not None:
                    out[key] = widget.property("plugin_out_of_range_value")
                else:
                    out[key] = widget.value()
            elif isinstance(widget, QCheckBox):
                out[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                data = widget.currentData()
                out[key] = data if data is not None else widget.currentText()
            elif isinstance(widget, QLineEdit) and widget.property("plugin_json_kind"):
                text = widget.text().strip()
                if not text:
                    # 留空视为「未提供」：省略该键，使运行前校验按未填写处理，
                    # 不再把空串当成类型不符。
                    continue
                try:
                    out[key] = loads_bounded_json(
                        text,
                        max_bytes=_MAX_JSON_PARAM_BYTES,
                        label=f"参数 {key}",
                    )
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"参数 {key} 不是合法 JSON：{exc}") from exc
            else:
                out[key] = widget.text()
        return out

    # ------------------------------------------------------------------
    def _on_accept_clicked(self):
        plugin_id = self._plugin_combo.currentData()
        action_id = self._action_combo.currentData()
        if not plugin_id or not action_id:
            QMessageBox.warning(self, "未选择", "请先选择插件与动作。")
            return
        manifest = self._registry.get_manifest(plugin_id) if self._registry else None
        action = self._registry.get_action(plugin_id, action_id) if self._registry else None
        if manifest is None or action is None:
            QMessageBox.critical(self, "插件不可用", "所选插件或动作当前不可用。")
            return

        try:
            config = self._collect_config()
        except ValueError as exc:
            # FR-20261003-603: array/object 参数填了非法 JSON 时给出明确提示，
            # 而不是静默转换或抛到调用方。
            QMessageBox.warning(self, "参数格式错误", str(exc))
            return
        missing = self._missing_required(action, config)
        if missing:
            QMessageBox.warning(
                self, "缺少必填参数", "请填写必填参数：\n" + "\n".join(missing)
            )
            return
        self._task["plugin_id"] = plugin_id
        self._task["plugin_action_id"] = action_id
        self._task["plugin_schema_version"] = manifest.api_version
        self._task["plugin_config"] = config
        self._task["value"] = f"{manifest.name}：{action.name}"
        self._task["type"] = TASK_TYPE_PLUGIN_ACTION
        self._on_accept(self._task)
        self.accept()

    @staticmethod
    def _missing_required(action, config: dict) -> list[str]:
        """返回 input_schema 中声明为 required 但当前为空的参数标题列表。

        仅字符串/文本类参数可能为空（None 或空串）；数值与布尔控件总有有效值，
        不视为缺失。标题取自 properties 的 title，回退为参数键名。
        """
        schema = getattr(action, "input_schema", {}) or {}
        if not isinstance(schema, dict):
            return []
        required_raw = schema.get("required", []) or []
        if not isinstance(required_raw, (list, tuple)):
            return []
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        missing: list[str] = []
        for key in required_raw:
            value = config.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                prop = properties.get(key)
                title = prop.get("title", key) if isinstance(prop, dict) else key
                missing.append(str(title))
        return missing
