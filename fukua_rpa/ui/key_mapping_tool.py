"""按键映射工具窗口（2026-10-19 迁移：设置 → 工具）。

定位：**映射配置编辑窗口**。
- 关闭只隐藏，不改变映射的启用状态（启用由裸键开关与每行规则决定，与窗口是否可见无关）；
- 重新打开复用同一窗口与同一批控件，不重复注册、不重复连接信号；
- 与「连点器 / 计划任务 / 录制」这类**运行类工具**不同：后者关闭即停止，前者只收界面。

实现要点：映射控件由宿主（RPAWindow）持有（C-1），本窗口负责把它们收揽进来。
宿主持有 Python 引用**不等于**持有 Qt 父对象所有权，因此这里显式重定父对象：
控件随「按键映射」折叠区一起加入本窗口布局，Qt 的所有权随之转移。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .components import HelpBtn, ResponsiveRow
from .lifecycle import is_runtime_editing_locked
from .settings_sections.mapping import build_mapping_controls


class KeyMappingToolWindow(QWidget):
    """「工具 → 按键映射」窗口：映射列表、启用状态、点击方式与目标窗口绑定。"""

    def __init__(self, host):
        super().__init__()
        self.host = host
        self._settings = getattr(host, "settings", None) or QSettings("fukuaRPA", "fukuaRPA")
        self.setWindowTitle("按键映射 · fukuaRPA")
        # 默认不置顶；几何持久化由 QSettings 负责。
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self._build_ui()
        self._restore_geometry()

    # ---------------- UI 构建（只做一次） ----------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        # UI-CONVENTION: allow UI004 reason=沿用计划任务/插件中心工具窗口的页边距与间距（scheduled_tasks_window.py:249-250），保持同类工具窗口观感一致
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header = ResponsiveRow()
        title = QLabel("按键映射")
        title.setProperty("role", "title")
        header.add_group(title)
        # 2026-10-21：映射已全局共享，不再随方案变化，因此不再显示「当前方案」。
        header.add_group(HelpBtn(self._header_help_text()))
        root.addWidget(header)

        note = QLabel(
            "按下映射热键后，软件会替你点击指定坐标。脚本或连点器正在运行时默认忽略映射，"
            "避免打断自动化流程。带 Ctrl/Alt/Shift 的组合键可直接全局生效；"
            "裸字母、裸数字、Space 等单键需要开启「按键映射模式」。"
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        root.addWidget(note)

        # 显式重定父对象：映射区（含宿主态控件）随本窗口布局加入，Qt 所有权随之转移。
        section = build_mapping_controls(self.host)
        section.setParent(self)
        # 让映射区占满窗口剩余高度：滚动区需要可扩展，否则只占内容高度、
        # 下方留出一大块空白。
        section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        root.addWidget(section, 1)
        self.mapping_section = section

    def _header_help_text(self) -> str:
        return (
            "【按键映射模式】\n只控制裸字母、裸数字、Space 这类单键映射，"
            "不是所有映射的总开关；组合键与每行启用规则不受它影响。\n"
            "【关闭窗口】\n只收起界面，不改变映射的启用状态；"
            "映射是否生效取决于本开关与每行的启停规则。"
        )

    # ---------------- 展示与状态 ----------------

    def refresh_profile_name(self):
        """保留调用点；映射不再随方案变化，窗口无需展示方案名。"""

    def show_and_raise(self):
        self.refresh_profile_name()
        # FR-20261022-001: 运行中新建的窗口也必须立即只读。入口按钮在运行期已禁用，
        # 这里覆盖测试或未来入口直接创建窗口的情形。
        if is_runtime_editing_locked(self.host):
            self.mapping_section.setEnabled(False)
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------------- 几何与关闭语义 ----------------

    def _restore_geometry(self):
        # UI-CONVENTION: allow fixed-geometry reason=工具窗口需要稳定的默认尺寸，
        # 首次打开按 sizeHint 弹出会挤成一条（映射行控件多、需要横向空间）。
        self.setMinimumSize(720, 480)
        raw = None
        if self._settings is not None:
            raw = self._settings.value("key_mapping_tool_geometry", None)
        if isinstance(raw, (bytes, bytearray)) and self.restoreGeometry(bytes(raw)):
            return
        # 无历史几何：给一个能完整显示「热键/坐标/绑定/动作」一行控件的大小。
        self.resize(1400, 530)

    def _save_geometry(self):
        if self._settings is None:
            return
        self._settings.setValue("key_mapping_tool_geometry", self.saveGeometry())
        with_sync = getattr(self._settings, "sync", None)
        if callable(with_sync):
            with_sync()

    def closeEvent(self, event):
        """关闭只隐藏：映射的启用状态与热键注册均不因关闭而改变。"""
        self._save_geometry()
        self.hide()
        event.ignore()
