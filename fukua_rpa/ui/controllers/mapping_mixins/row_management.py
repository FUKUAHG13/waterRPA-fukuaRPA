# pyright: reportAttributeAccessIssue=false
"""按键映射行管理 Mixin（God Class 拆分）。

从 ``MappingController`` 搬移的映射行构建、信号连接、数据套用、
删除与配置读写方法组。行控件由宿主窗口的 ``key_mapping_rows`` 持有，
Mixin 自身不定义状态；``bulk_update`` 由主控制器 ``__init__`` 创建。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
)

from ....constants import DEFAULT_KEY_MAPPING_COUNT, MAX_KEY_MAPPINGS
from ....log_policy import (
    LOG_APPLICATION,
)
from ....logging_service import write_log
from ....task_model import config_bool
from ....win32_api import parse_hotkey_text
from ...components import (
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)

logger = logging.getLogger(__name__)


class MappingRowMixin:
    """按键映射行构建与配置读写（9 方法）。"""

    #: 可解析的 F 键上限：HOTKEY_NAME_TO_VK 只覆盖 F1–F12，F13 及以上无法解析。
    _MAX_DEFAULT_FKEY = 12

    def _next_default_mapping_hotkey(self):
        """Return a default hotkey that is guaranteed to parse.

        旧实现用 ``F{行数+1}``：行数超过 12 后生成 F13/F25 这类无法解析的名字，
        保存进方案后会让**整份方案**在切换时校验失败（表现为「热键格式无效」、
        方案不可用且无法进入界面修正）。这里改为「取第一个未被占用的 F1–F12」，
        全部占满时返回空串，由用户自行录入，绝不生成非法默认值。
        """
        used = {
            str(row.get("hotkey").text() or "").strip().upper()
            for row in getattr(self.host, "key_mapping_rows", [])
            if row.get("hotkey") is not None
        }
        for index in range(1, self._MAX_DEFAULT_FKEY + 1):
            candidate = f"F{index}"
            if candidate.upper() not in used:
                return candidate
        return ""

    def add_key_mapping_row(self, data=None, refresh=True):
        idx = len(getattr(self.host, "key_mapping_rows", []))
        container = ResponsiveRow(horizontal_spacing=10, vertical_spacing=7)
        container.setProperty("rowRole", "keyMapping")

        en = QCheckBox()
        hotkey = QLineEdit(self._next_default_mapping_hotkey())
        hotkey.setPlaceholderText("A / Ctrl+A / F1")
        hotkey.setMinimumWidth(125)

        key_btn = fit_text_button(QPushButton("录键"), 64)
        key_btn.setProperty("variant", "ghost")
        key_btn.setToolTip("录入映射热键；完成后焦点会回到设置窗口")
        container.add_group(en, "热键:", hotkey, key_btn)

        coord = QLineEdit("")
        coord.setPlaceholderText("例如 960,540")
        coord.setMinimumWidth(150)

        pick = fit_text_button(QPushButton("取点"), 64)
        pick.setProperty("variant", "tonal")
        pick.setToolTip("选取点击坐标")

        bind_window = fit_text_button(QPushButton("选择窗口"), 96)
        bind_window.setProperty("variant", "ghost")
        bind_window.setToolTip(
            "仅用于后台窗口点击：先填写或取点，再点此按钮并亲自单击目标程序窗口"
        )
        container.add_group("坐标:", coord, pick)

        binding_status = QLabel("未绑定目标窗口")
        binding_status.setProperty("role", "muted")
        binding_status.setMinimumWidth(110)
        binding_status.setMaximumWidth(520)
        container.add_group(bind_window)

        action = NoWheelComboBox()
        action.addItems(["左键单击", "左键双击", "右键单击"])
        fit_combo_to_contents(action)

        delete_btn = QPushButton("")
        delete_btn.setIcon(self.host.style().standardIcon(QStyle.SP_TrashIcon))
        delete_btn.setProperty("iconOnly", True)
        delete_btn.setProperty("variant", "dangerGhost")
        inspect_btn = QPushButton("")
        inspect_btn.setIcon(self.host.style().standardIcon(QStyle.SP_MessageBoxInformation))
        inspect_btn.setProperty("iconOnly", True)
        inspect_btn.setProperty("variant", "ghost")
        inspect_btn.setToolTip("检查已绑定窗口、目标控件和权限兼容性")
        container.add_group(action, inspect_btn, delete_btn)
        binding_group = container.add_trailing_group(binding_status)

        row_data = {
            "container": container, "enabled": en, "hotkey": hotkey, "key_btn": key_btn,
            "coord": coord, "pick": pick, "bind_window": bind_window, "action": action,
            "binding_status": binding_status, "binding_group": binding_group,
            "inspect_btn": inspect_btn, "delete_btn": delete_btn, "window_binding": {}
        }

        self._connect_mapping_row_signals(row_data)

        self.host.key_mapping_rows.append(row_data)
        # Prime immutable geometry bases before the row inherits a secondary
        # screen's local stylesheet.  The second call below then composes the
        # stored source sizes with the actual target-screen density exactly once.
        self.host.apply_ui_scale_to_widget(container)
        self.host.mapping_rows_layout.addWidget(container)
        if data is not None:
            self.host.apply_key_mapping_row_data(row_data, data, idx)
        else:
            self.host.update_mapping_window_binding_ui(row_data)
        self.host.refresh_mapping_row_labels()
        self._ensure_mapping_rows_stretch()
        if refresh:
            self.host.refresh_hotkey_backend()
        self.host.apply_ui_scale_to_widget(container)
        return row_data

    def _connect_mapping_row_signals(self, row_data):
        """Wire the per-row clicked/textChanged/editingFinished signals.

        Extracted from add_key_mapping_row so that method stays focused on
        widget construction and layout. Widgets are read back from row_data;
        signal targets and connection order are unchanged.
        """
        en = row_data["enabled"]
        hotkey = row_data["hotkey"]
        coord = row_data["coord"]
        action = row_data["action"]
        key_btn = row_data["key_btn"]
        pick = row_data["pick"]
        bind_window = row_data["bind_window"]
        inspect_btn = row_data["inspect_btn"]
        delete_btn = row_data["delete_btn"]

        key_btn.clicked.connect(lambda _=False, r=row_data: self.host.capture_mapping_hotkey_by_row(r))
        pick.clicked.connect(lambda _=False, r=row_data: self.host.start_mapping_coordinate_pick_by_row(r))
        bind_window.clicked.connect(lambda _=False, r=row_data: self.host.bind_mapping_window_by_row(r))
        inspect_btn.clicked.connect(
            lambda _=False, r=row_data: self.host.show_mapping_window_inspector(r)
        )
        delete_btn.clicked.connect(lambda _=False, r=row_data: self.host.remove_key_mapping_row(r))
        coord.textChanged.connect(lambda _text, r=row_data: self.host.clear_mapping_window_binding(r))
        for widget in [en, hotkey, coord, action]:
            if isinstance(widget, QLineEdit):
                widget.editingFinished.connect(self.host.refresh_hotkey_backend)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.host.refresh_hotkey_backend)
            else:
                widget.stateChanged.connect(self.host.refresh_hotkey_backend)

    def refresh_mapping_hotkey_failure_notice(self) -> None:
        """Show or hide the mapping-hotkey failure notice to match reality.

        FR-SCOPE009-002: the label is driven by ``mapping_registration_failures``
        rather than by a one-shot message, so it cannot drift from the actual
        registration state: no failures -> hidden, any failure -> visible with the
        affected mappings named.
        """
        label = getattr(self.host, "mapping_hotkey_failure_notice", None)
        if label is None:
            return
        controller = getattr(self.host, "hotkey_controller", None)
        failures = getattr(controller, "mapping_registration_failures", None) or {}
        if failures:
            # 直接写出组合键文本（失败字典的值就是它），用户才能据此核对；
            # 只给序号的话还得自己回去数是哪一条。
            hotkeys = "、".join(
                str(value) for _key, value in sorted(failures.items(), key=lambda kv: str(kv[0]))
            )
            label.setText(
                "以下映射热键注册失败（可能已被其他程序占用），本次运行不生效："
                f"{hotkeys}。请更换组合键后重试。"
            )
            label.setVisible(True)
        else:
            label.clear()
            label.setVisible(False)

    def apply_key_mapping_row_data(self, row, data, idx=0):
        data = data if isinstance(data, dict) else {}
        row["enabled"].setChecked(config_bool(data.get("enabled", False)))
        parsed = parse_hotkey_text(data.get("hotkey", f"F{idx + 1}"))
        row["hotkey"].setText(parsed["display"] if parsed else str(data.get("hotkey", f"F{idx + 1}")))
        row["coord"].setText(str(data.get("coord", "")))
        binding = data.get("window_binding", {})
        row["window_binding"] = dict(binding) if isinstance(binding, dict) else {}
        self.host.update_mapping_window_binding_ui(row)
        row["action"].setCurrentText(str(data.get("action", "左键单击")))

    def clear_key_mapping_rows(self):
        for row in list(getattr(self.host, "key_mapping_rows", [])):
            container = row.get("container")
            if container:
                try:
                    self.host.mapping_rows_layout.removeWidget(container)
                    container.setParent(None)
                    container.deleteLater()
                except Exception as exc:
                    from ....logging_service import write_log

                    write_log(f"映射操作失败: {exc}",
                                 category=LOG_APPLICATION,
                                 critical=True,
                             )
        self.host.key_mapping_rows = []
        # 清空时必须连 spacer 一起移除：旧实现只删控件，残留的弹簧会让新行之间
        # 各夹一个伸缩项，表现为「行间距异常大」。
        layout = getattr(self.host, "mapping_rows_layout", None)
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
        self._ensure_mapping_rows_stretch()

    def _ensure_mapping_rows_stretch(self):
        """Keep a trailing stretch so spare height stays at the bottom.

        缺它时 QVBoxLayout 会把滚动区内的剩余高度分摊到各映射行之间，
        表现为「行间距异常大、而且行数越少越明显」。
        """
        layout = getattr(self.host, "mapping_rows_layout", None)
        if layout is None:
            return
        # 先清掉所有旧弹簧：每加一行都调本函数，若只在末尾追加，旧弹簧会留在
        # 行与行之间，剩余高度被分摊成「大间距」。
        for index in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                layout.takeAt(index)
        layout.addStretch(1)

    def refresh_mapping_row_labels(self):
        for idx, row in enumerate(getattr(self.host, "key_mapping_rows", [])):
            try:
                row["enabled"].setText(f"映射{idx + 1}")
                _align_mapping_index_label(row["enabled"])
                row["delete_btn"].setToolTip(f"删除映射{idx + 1}")
            except Exception as exc:
                logger.debug("更新映射行提示失败: %s", exc, exc_info=True)
                pass

    def mapping_row_index(self, row_data):
        try:
            return self.host.key_mapping_rows.index(row_data)
        except ValueError:
            return -1

    def remove_key_mapping_row(self, row_data):
        idx = self.host.mapping_row_index(row_data)
        if idx < 0:
            return
        container = row_data.get("container")
        self.host.key_mapping_rows.pop(idx)
        if container:
            try:
                self.host.mapping_rows_layout.removeWidget(container)
                container.setParent(None)
                container.deleteLater()
            except Exception as exc:
                logger.debug("删除映射容器失败: %s", exc, exc_info=True)
                pass
        self.host.refresh_mapping_row_labels()
        self.host.refresh_hotkey_backend()

    def get_key_mappings_config(self):
        mappings = []
        for row in getattr(self.host, "key_mapping_rows", []):
            mappings.append({
                "enabled": row["enabled"].isChecked(),
                "hotkey": row["hotkey"].text().strip(),
                "coord": row["coord"].text().strip(),
                "action": row["action"].currentText(),
                "window_binding": dict(row.get("window_binding", {})),
            })
        return mappings

    def apply_key_mappings_config(self, mappings, desired_count=None):
        mappings = mappings if isinstance(mappings, list) else []
        if len(mappings) > MAX_KEY_MAPPINGS:
            write_log(f"按键映射数量超过上限 {MAX_KEY_MAPPINGS}，多余内容已忽略。",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            mappings = mappings[:MAX_KEY_MAPPINGS]
        try:
            count = int(float(desired_count)) if desired_count is not None else (len(mappings) if mappings else DEFAULT_KEY_MAPPING_COUNT)
        except Exception as exc:
            logger.debug("解析映射数量失败: %s", exc, exc_info=True)
            count = len(mappings) if mappings else DEFAULT_KEY_MAPPING_COUNT
        count = min(MAX_KEY_MAPPINGS, max(0, max(count, len(mappings))))
        self.bulk_update = True
        try:
            self.host.clear_key_mapping_rows()
            for idx in range(count):
                data = mappings[idx] if idx < len(mappings) and isinstance(mappings[idx], dict) else None
                self.host.add_key_mapping_row(data=data, refresh=False)
            self.host.refresh_mapping_row_labels()
        finally:
            self.bulk_update = False
        self.host.refresh_mapping_binding_controls()
        self.host.refresh_hotkey_backend()


def _align_mapping_index_label(label):
    """Pin the index label to two-digit width so 映射1..映射99 line up.

    之前只设 minimumWidth：两位数文字本身比一位数宽，最小宽度挡不住它继续变宽，
    于是「热键」列在 映射10 处整体右移。改为按「映射99」实测宽度**固定**宽度，
    并预留复选框指示器与间距。
    """
    metrics = label.fontMetrics()
    indicator = 18
    padding = 8
    # 一位数向两位数对齐；三位数（映射100+）按实际文本放宽，避免被裁切。
    width = max(
        metrics.horizontalAdvance("映射99"), metrics.horizontalAdvance(label.text())
    ) + indicator + padding
    if label.minimumWidth() != width or label.maximumWidth() != width:
        # UI-CONVENTION: allow UI003 reason=序号列必须定宽才能让 映射1 与 映射10 的
        # 「热键」列对齐；宽度由字体度量实时算出，不是写死像素。
        label.setFixedWidth(width)
