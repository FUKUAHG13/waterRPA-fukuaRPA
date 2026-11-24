# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""「鼠标轨迹」步骤的采样点编辑区（小齿轮设置内）。

行式编辑已有采样点的相对毫秒 / X / Y，草稿与已保存参数隔离；校验与序列化
复用纯模型 :mod:`fukua_rpa.trajectory_editor`。写回、撤销与运行锁由已有的
步骤设置对话框生命周期负责，本模块只维护对话框工作副本中的 ``trajectory``
草稿与对应行控件。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..trajectory_editor import (
    format_trajectory_summary,
    parse_trajectory,
    serialize_trajectory,
    validate_points,
)
from .components import CollapsibleSection

# 单次可见的行控件上限；超出用滚动区浏览，不随轨迹长度无界创建控件。
_VISIBLE_ROW_LIMIT = 12

# 三列固定列名的常驻说明（v1.1.19）。坐标可为负值，沿用现有输入约定。
_COLUMN_HEADER_TEXT = ("轨迹内时刻（毫秒）", "屏幕 X（横向）", "屏幕 Y（纵向）")
_HEADER_EXPLAINER = (
    "每行是一个采样点，时刻从本条轨迹开始计算。"
    "例如 6 / 1948 / 543 表示开始后第 6 毫秒，鼠标移动到屏幕坐标 (1948, 543)。"
    "屏幕坐标可为负值（多显示器布局）。"
)


class _HeaderAlignFilter(QObject):
    """在滚动区显隐/尺寸变化时同步表头右边距，不重建控件。

    UI 规范禁止"只在 resizeEvent 无限重建布局"；这里只更新一次
    ``setContentsMargins`` 数值，且值未变化时跳过，不重建表头行。
    """

    def __init__(self, controller):
        super().__init__()
        self._controller = controller

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Show):
            self._controller.sync_header_margin()
        return False


class TrajectorySectionController:
    """管理一个对话框内的轨迹草稿点与行控件同步。"""

    def __init__(self, data):
        raw = str(data.get("trajectory", "") or "")
        parsed = parse_trajectory(raw)
        # 非法/空轨迹以空草稿呈现，保留原始字节于 original_raw，取消时不动。
        self.original_raw = raw
        self.points: list[tuple[int, int, int]] = (
            list(parsed.points) if parsed.ok and parsed.points else []
        )
        self.rows: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        self.rows_host: QWidget | None = None
        self.summary_label: QLabel | None = None
        self.status_label: QLabel | None = None
        self.header_widget: QWidget | None = None
        self.header_labels: list[QLabel] = []
        self.scroll_area: QScrollArea | None = None
        self.error_line = 0

    def sync_header_margin(self):
        """表头三列与数据行同宽：右侧预留滚动条宽度（显隐/缩放时同步）。"""
        if self.header_widget is None or self.scroll_area is None:
            return
        scroll_bar = self.scroll_area.verticalScrollBar()
        # 动态读取滚动条宽度，不写死像素；滚动条未出现时为 0。
        margin = scroll_bar.sizeHint().width() if scroll_bar.isVisible() else 0
        layout = self.header_widget.layout()
        current = layout.contentsMargins()
        if current.right() == margin:
            return
        # UI-CONVENTION: allow layout-metrics reason=表头右侧预留滚动条宽度以与数据行三列对齐
        layout.setContentsMargins(
            current.left(), current.top(), margin, current.bottom()
        )

    # ---- 行控件 ----
    def build_rows(self, rows_host):
        self.rows_host = rows_host
        layout = rows_host.layout()
        for point in self.points:
            self._append_row(layout, point)
        self.refresh_summary()

    def _append_row(self, layout, point=None):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        # UI-CONVENTION: allow layout-metrics reason=采样点三列需要紧凑间隔以对齐且避免误点相邻输入框
        row_layout.setSpacing(6)
        t_edit = QLineEdit()
        x_edit = QLineEdit()
        y_edit = QLineEdit()
        if point is not None:
            t_edit.setText(str(point[0]))
            x_edit.setText(str(point[1]))
            y_edit.setText(str(point[2]))
        t_edit.setPlaceholderText("毫秒")
        x_edit.setPlaceholderText("X")
        y_edit.setPlaceholderText("Y")
        t_edit.setObjectName("trajectory_t")
        x_edit.setObjectName("trajectory_x")
        y_edit.setObjectName("trajectory_y")
        row_layout.addWidget(t_edit)
        row_layout.addWidget(x_edit)
        row_layout.addWidget(y_edit)
        layout.addWidget(row)
        self.rows.append((t_edit, x_edit, y_edit))

    def add_point(self):
        self.points.append((0, 0, 0))
        if self.rows_host is not None:
            self._append_row(self.rows_host.layout(), (0, 0, 0))
        self.refresh_summary()

    def remove_last_point(self):
        if not self.points:
            return
        self.points.pop()
        if self.rows and self.rows_host is not None:
            _row_widget = self.rows[-1][0].parentWidget()
            self.rows.pop()
            if _row_widget is not None:
                self.rows_host.layout().removeWidget(_row_widget)
                _row_widget.deleteLater()
        self.refresh_summary()

    def sync_from_rows(self):
        """把行控件当前文本读回草稿点；失败返回带行号的原因。"""
        points = []
        for index, (t_edit, x_edit, y_edit) in enumerate(self.rows, start=1):
            try:
                t = int(t_edit.text().strip())
                x = int(x_edit.text().strip())
                y = int(y_edit.text().strip())
            except ValueError:
                self.error_line = index
                return f"第 {index} 行必须为整数"
            points.append((t, x, y))
        result = validate_points(points)
        if not result.ok:
            self.error_line = result.line or 0
            return result.reason
        self.points = list(result.points or points)
        self.error_line = 0
        self.refresh_summary()
        return ""

    def refresh_summary(self):
        if self.summary_label is not None:
            self.summary_label.setText(format_trajectory_summary(self.points))

    def set_status(self, text):
        if self.status_label is not None:
            self.status_label.setText(text)

    def commit_to(self, data):
        """把草稿写回对话框工作副本；仅在校验通过时调用。"""
        data["trajectory"] = serialize_trajectory(self.points)
        data["value"] = format_trajectory_summary(self.points)


def build_trajectory_section(dialog, data, layout):
    """构建采样点编辑 section；始终创建，显隐由当前动作类型驱动。"""
    controller = TrajectorySectionController(data)

    section = CollapsibleSection("轨迹采样点", expanded=True)
    section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    section_layout = QVBoxLayout()
    # UI-CONVENTION: allow layout-metrics reason=摘要、滚动列表与按钮之间需要可见分隔以区分功能区
    section_layout.setSpacing(8)

    summary = QLabel(format_trajectory_summary(controller.points))
    summary.setProperty("role", "muted")
    summary.setWordWrap(True)
    controller.summary_label = summary

    status = QLabel("")
    status.setProperty("role", "muted")
    status.setWordWrap(True)
    controller.status_label = status

    explainer = QLabel(_HEADER_EXPLAINER)
    explainer.setProperty("role", "muted")
    explainer.setWordWrap(True)

    # 固定表头：与 _append_row 完全相同的 QHBoxLayout(margins 0, spacing 6)
    # 和 Expanding 水平策略，确保三列与数据行对齐且滚动时保持可见。
    header = QWidget()
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(0, 0, 0, 0)
    # UI-CONVENTION: allow layout-metrics reason=表头三列需与采样行三列同间隔对齐
    header_layout.setSpacing(6)
    header_object_names = ("trajectory_header_t", "trajectory_header_x", "trajectory_header_y")
    for index, text in enumerate(_COLUMN_HEADER_TEXT):
        label = QLabel(text)
        label.setProperty("role", "muted")
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        label.setObjectName(header_object_names[index])
        header_layout.addWidget(label)
        controller.header_labels.append(label)
    controller.header_widget = header

    rows_host = QWidget()
    rows_layout = QVBoxLayout(rows_host)
    rows_layout.setContentsMargins(0, 0, 0, 0)
    # UI-CONVENTION: allow layout-metrics reason=采样点逐行排列需要紧凑间隔，长轨迹靠滚动区承载
    rows_layout.setSpacing(6)
    rows_layout.setAlignment(Qt.AlignTop)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(rows_host)
    # UI-CONVENTION: allow fixed-geometry reason=采样点列表需稳定可视高度，超出部分滚动浏览避免撑满对话框
    scroll.setMaximumHeight(260)
    controller.scroll_area = scroll
    controller.build_rows(rows_host)
    # 表头右边距随滚动条显隐同步；事件过滤器同时覆盖 show/resize，
    # 只更新 contentsMargins 数值、不重建表头行（UI 规范第 141 行）。
    scroll.verticalScrollBar().rangeChanged.connect(
        lambda *_args: controller.sync_header_margin()
    )
    align_filter = _HeaderAlignFilter(controller)
    scroll.installEventFilter(align_filter)
    header.installEventFilter(align_filter)
    dialog._trajectory_header_align_filter = align_filter

    buttons = QWidget()
    buttons_layout = QHBoxLayout(buttons)
    buttons_layout.setContentsMargins(0, 0, 0, 0)
    add_btn = QPushButton("添加采样点")
    remove_btn = QPushButton("删除末点")
    buttons_layout.addWidget(add_btn)
    buttons_layout.addWidget(remove_btn)
    buttons_layout.addStretch(1)

    add_btn.clicked.connect(controller.add_point)
    remove_btn.clicked.connect(controller.remove_last_point)

    section_layout.addWidget(summary)
    section_layout.addWidget(explainer)
    section_layout.addWidget(header)
    section_layout.addWidget(scroll)
    section_layout.addWidget(buttons)
    section_layout.addWidget(status)
    section.set_content_layout(section_layout)
    layout.addWidget(section)

    dialog.trajectory_section = section
    dialog.trajectory_controller = controller
    return section
