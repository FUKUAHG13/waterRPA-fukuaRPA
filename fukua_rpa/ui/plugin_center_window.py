# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false, reportReturnType=false

"""Plugin center window (design section 5.2).

A non-modal, single-instance-friendly top-level window for managing locally
installed plugins: install from a ``.fkrplugin`` file, enable/disable, uninstall
(with a referenced-step guard), open the plugin directory, and view developer
info. It does NOT talk to any network — it is a local management UI only.

The window owns a :class:`PluginController` (which wraps the app-level
:class:`PluginRegistry`). All plugin mutations go through the controller so the
table refreshes consistently.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .components import (
    FloatingSettingsDialog,
    NoWheelComboBox,
    fit_combo_to_contents,
    fit_text_button,
)
from .controllers.plugin_controller import (
    STATUS_LABELS,
    TYPE_LABELS,
    PluginController,
)

logger = logging.getLogger(__name__)


class PluginCenterWindow(FloatingSettingsDialog):
    """Top-level plugin management browser."""

    def __init__(
        self,
        controller: PluginController,
        install_base_dir: str,
        settings,
        parent=None,
    ):
        super().__init__(settings, "plugin_center_geometry", "插件中心", (900, 620))
        self._controller = controller
        self._install_base_dir = install_base_dir
        self._build_ui()
        self._restore_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_filter_bar())
        root.addWidget(self._build_table(), 1)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._btn_install = fit_text_button(QPushButton("从本地安装"))
        self._btn_refresh = fit_text_button(QPushButton("刷新"))
        self._btn_toggle = fit_text_button(QPushButton("启用/停用"))
        self._btn_open_dir = fit_text_button(QPushButton("打开插件目录"))
        self._btn_devinfo = fit_text_button(QPushButton("开发者信息"))

        self._btn_install.clicked.connect(self._on_install)
        self._btn_refresh.clicked.connect(lambda: self._controller.rescan())
        self._btn_toggle.clicked.connect(self._on_toggle_selected)
        self._btn_open_dir.clicked.connect(self._on_open_dir)
        self._btn_devinfo.clicked.connect(self._on_dev_info)

        for btn in (
            self._btn_install,
            self._btn_refresh,
            self._btn_toggle,
            self._btn_open_dir,
            self._btn_devinfo,
        ):
            layout.addWidget(btn)
        layout.addStretch(1)
        return bar

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索插件名称或作者…")
        self._search_edit.textChanged.connect(self._on_search_changed)

        self._status_combo = NoWheelComboBox()
        for value, label in [
            ("all", "全部"),
            ("enabled", "已启用"),
            ("disabled", "已停用"),
            ("incompatible", "不兼容"),
            ("corrupt", "损坏"),
        ]:
            self._status_combo.addItem(label, value)
        self._status_combo.setCurrentIndex(0)
        fit_combo_to_contents(self._status_combo)
        self._status_combo.currentIndexChanged.connect(self._on_status_changed)

        layout.addWidget(QLabel("筛选："))
        layout.addWidget(self._search_edit, 1)
        layout.addWidget(self._status_combo)
        return bar

    def _build_table(self) -> QTableView:
        table = QTableView(self)
        table.setObjectName("pluginCenterTable")
        table.setModel(self._controller.model())
        self._controller.model().modelReset.connect(self._on_model_reset)
        self._controller.model().orderChanged.connect(self._on_row_order_changed)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)

        # 列头可自由拖动调整列顺序（像任务管理器那样），顺序跨重启保存。
        table.horizontalHeader().setSectionsMovable(True)

        # 行可拖动重排（编号列由此自动重排）；仅在无筛选时启用，避免隐藏行被误排。
        table.setDragEnabled(True)
        table.setAcceptDrops(True)
        table.setDragDropMode(QAbstractItemView.InternalMove)
        table.setDropIndicatorShown(True)

        # 原“操作”列功能改为右键菜单。
        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(self._on_context_menu)

        # 默认列宽（首次打开或无保存状态时使用）
        _DEFAULT_COL_WIDTHS = {
            0: 42,    # #
            1: 160,   # 名称
            2: 70,    # 版本
            3: 90,    # 作者
            4: 70,    # 类型
            5: 70,    # 状态
            6: 130,   # 能力
            7: 100,   # 使用情况
            8: 180,   # 最近错误
        }
        for col, width in _DEFAULT_COL_WIDTHS.items():
            table.setColumnWidth(col, width)
        return table

    def _table(self) -> QTableView:
        return self.findChild(QTableView, "pluginCenterTable")

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def _on_search_changed(self, text: str) -> None:
        self._apply_filter()

    def _on_status_changed(self, _index: int) -> None:
        self._apply_filter()

    def _on_model_reset(self) -> None:
        # SAFETY: _apply_filter calls endResetModel which re-emits modelReset;
        # guard prevents infinite recursion.
        if getattr(self, "_filtering", False):
            return
        self._apply_filter()

    def _apply_filter(self) -> None:
        self._filtering = True
        try:
            model = self._controller.model()
            text = self._search_edit.text().strip().lower()
            status = self._status_combo.currentData()
            rows = model.all_rows()
            filtered = []
            for rec in rows:
                if status != "all" and rec.status.value != status:
                    continue
                if text and text not in (rec.name + " " + rec.author).lower():
                    continue
                filtered.append(rec)
            model.set_rows(filtered)
        finally:
            self._filtering = False

        # 仅在无筛选时允许行拖动重排，避免隐藏行参与重排导致顺序错乱。
        table = self._table()
        if table is not None:
            no_filter = status == "all" and not text
            table.setDragEnabled(no_filter)

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------
    def _on_install(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "安装插件", "", "fukuaRPA 插件 (*.fkrplugin);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            manifest = self._controller.install_from_file(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "安装失败", f"插件安装被拒绝：\n{exc}")
            return
        QMessageBox.information(
            self,
            "安装完成",
            f"已安装插件“{manifest.name}” (v{manifest.version})。\n"
            "请检查其权限与动作，并在需要时手动启用。",
        )

    def _selected_plugin_ids(self) -> list[str]:
        table = self._table()
        if table is None:
            return []
        ids = []
        for index in table.selectionModel().selectedRows():
            pid = self._controller.model().plugin_id_at(index)
            if pid:
                ids.append(pid)
        return ids

    def _on_toggle_selected(self) -> None:
        ids = self._selected_plugin_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择至少一个插件。")
            return
        for pid in ids:
            rec = self._record(pid)
            if rec is None:
                continue
            if rec.status.value == "enabled":
                # FR-20260809-1205: 与 enable 对称的异常兜底——插件目录在操作间隙
                # 被外部删除等场景下 PluginRegistry.disable 抛错不得逃逸到 Qt 槽。
                try:
                    self._controller.disable(pid)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "无法禁用", f"{pid}：{exc}")
            else:
                try:
                    self._controller.enable(pid)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "无法启用", f"{pid}：{exc}")

    def _record(self, plugin_id: str):
        for rec in self._controller.model().all_rows():
            if rec.plugin_id == plugin_id:
                return rec
        return None

    def _on_open_dir(self) -> None:
        url = QUrl.fromLocalFile(os.path.abspath(self._install_base_dir))
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "打开失败", "无法打开插件目录。")

    def _on_dev_info(self) -> None:
        QMessageBox.information(
            self,
            "开发者信息",
            "fukuaRPA 插件为本地离线安装，不提供在线商店、自动更新或遥测。\n\n"
            "插件包格式：.fkrplugin（有界 ZIP，含 manifest.json）。\n"
            "第一版插件类型为声明式（模板 / 预设），可执行代码插件将在隔离的 "
            "WASM 宿主中运行，且默认不提供任何网络能力。",
        )

    # ------------------------------------------------------------------
    # Row action menu
    # ------------------------------------------------------------------
    def _on_context_menu(self, pos) -> None:
        table = self._table()
        if table is None:
            return
        index = table.indexAt(pos)
        if not index.isValid():
            return
        self._open_row_menu(index)

    def _open_row_menu(self, index) -> None:
        pid = self._controller.model().plugin_id_at(index)
        if not pid:
            return
        rec = self._record(pid)
        if rec is None:
            return
        menu = QMenu(self)
        act_detail = menu.addAction("详情")
        act_toggle = menu.addAction("启用" if rec.status.value != "enabled" else "停用")
        menu.addSeparator()
        act_uninstall = menu.addAction("卸载")

        choice = menu.exec(self.cursor().pos())
        if choice is None:
            return
        if choice is act_detail:
            self._show_detail(rec)
        elif choice is act_toggle:
            if rec.status.value == "enabled":
                # FR-20260809-1205: 右键菜单路径的 disable 同样需异常兜底。
                try:
                    self._controller.disable(pid)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "无法禁用", f"{pid}：{exc}")
            else:
                try:
                    self._controller.enable(pid)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "无法启用", f"{pid}：{exc}")
        elif choice is act_uninstall:
            self._confirm_uninstall(pid)

    def _show_detail(self, rec) -> None:
        lines = [
            f"名称：{rec.name or rec.plugin_id}",
            f"ID：{rec.plugin_id}",
            f"版本：{rec.version}（API {rec.api_version}）",
            f"作者：{rec.author}",
            f"类型：{TYPE_LABELS.get(rec.plugin_type, rec.plugin_type)}",
            f"状态：{STATUS_LABELS.get(rec.status, rec.status.value)}",
            f"声明权限：{', '.join(rec.permissions) if rec.permissions else '无'}",
            f"动作数：{rec.action_count}  模板数：{rec.template_count}",
        ]
        if rec.dependencies:
            lines.append(f"依赖：{', '.join(rec.dependencies)}")
        if rec.status.value == "corrupt" and rec.corrupt_reason:
            lines.append(f"损坏原因：{rec.corrupt_reason}")
        QMessageBox.information(self, f"插件详情 · {rec.name or rec.plugin_id}", "\n".join(lines))

    def _confirm_uninstall(self, plugin_id: str) -> None:
        count = self._controller.usage_count(plugin_id)
        if count > 0:
            msg = (
                f"插件“{plugin_id}”被 {count} 个方案步骤引用。"
                "卸载后引用该插件的步骤将无法执行。确定要卸载吗？"
            )
        else:
            msg = f"确定要卸载插件“{plugin_id}”吗？"
        reply = QMessageBox.question(
            self,
            "确认卸载",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            ok, ref_count = self._controller.uninstall(plugin_id)
        except Exception as exc:
            # SAFETY: 插件目录被占用/杀软锁定等 OSError 不得逃逸到 Qt 槽——
            # 与 _on_toggle_selected 的 FR-20260809-1205 兜底同标准；失败必须
            # 可见，rmtree 可能已部分删除，注册表未更新由下次 scan 呈现。
            # （FR-20261010-2602）
            logger.warning("卸载插件 %s 失败：%s", plugin_id, exc)
            QMessageBox.warning(
                self,
                "卸载失败",
                f"插件“{plugin_id}”卸载失败：{exc}\n\n"
                "插件目录或文件可能正被占用（例如资源管理器或杀毒软件）。"
                "请关闭占用后重试；若插件显示异常状态，可再次尝试卸载。",
            )
            return
        if not ok:
            QMessageBox.warning(
                self,
                "无法卸载",
                f"插件“{plugin_id}”仍被 {ref_count} 个方案步骤引用。\n\n"
                "设计规则禁止静默删除被引用的步骤。请先移除或改写引用该插件的步骤，"
                "或将其停用并保留步骤。",
            )
            return
        QMessageBox.information(self, "卸载完成", f"插件“{plugin_id}”已卸载。")

    # ------------------------------------------------------------------
    # 跨重启持久化：列顺序/列宽（表头状态）+ 行顺序（拖拽编号）
    # ------------------------------------------------------------------
    def _restore_state(self) -> None:
        table = self._table()
        if table is None:
            return
        # 行顺序（手动拖拽后的插件排列）
        try:
            raw_order = self.settings.value("plugin_center_row_order")
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取插件顺序失败: %s", exc, exc_info=True)
            raw_order = None
        if raw_order:
            try:
                order = [str(i) for i in raw_order]
                self._controller.model().set_order(order)
            except Exception as exc:  # noqa: BLE001
                logger.debug("应用插件顺序失败: %s", exc, exc_info=True)
        # 列顺序与列宽（表头状态）
        try:
            state = self.settings.value("plugin_center_header_state")
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取表头状态失败: %s", exc, exc_info=True)
            state = None
        if isinstance(state, QByteArray):
            table.horizontalHeader().restoreState(state)

    def _save_row_order(self) -> None:
        try:
            self.settings.setValue(
                "plugin_center_row_order",
                self._controller.model().get_order(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存插件顺序失败: %s", exc, exc_info=True)

    def _on_row_order_changed(self) -> None:
        self._save_row_order()

    def closeEvent(self, event):
        table = self._table()
        if table is not None:
            try:
                self.settings.setValue(
                    "plugin_center_header_state",
                    table.horizontalHeader().saveState(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("保存表头状态失败: %s", exc, exc_info=True)
            self._save_row_order()
        super().closeEvent(event)
