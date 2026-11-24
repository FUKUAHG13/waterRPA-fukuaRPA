# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""输入时序行下方的编辑面板：以步骤列表为主视图，按需展开详情。

面板只编辑副本；取消丢弃副本，应用由控制器一次写回并生成一条工作流撤销记录。
折叠、筛选与缩放都是视图投影，不写入事件；原始事件下标是唯一身份。
长录制按可见时间范围加载行，不为每个移动点创建完整步骤控件。
预览只绘制轨迹与虚拟光标，真实键鼠回放仍走主流程运行入口。
"""
from __future__ import annotations

import copy

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QHelpEvent, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollBar,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..timeline_editor import (
    ActionGroup,
    DisplayRow,
    adjust_wait,
    build_display_rows,
    delete_action,
    edit_click_position,
    find_wait_gaps,
    group_events,
    timeline_source_label,
    validate_edited_timeline,
)
from . import theme
from .components import HelpBtn
from .lifecycle import widget_is_alive

# DESIGN: 预览画布是短暂可视反馈，颜色从 theme 的语义 token 派生，不在本页复制
# 一套浅色/深色规则。原始坐标映射为逻辑坐标，不掺入 UI 视觉倍率。
_PREVIEW_LINE = QColor(*theme.PREVIEW_LINE_SOLID)
_PREVIEW_ACTIVE = QColor(*theme.PREVIEW_LINE_ACTIVE)
_PREVIEW_CURSOR = QColor(*theme.INPUT_PANEL_ACCENT)
_PREVIEW_MUTED = QColor(*theme.INPUT_PANEL_MUTED)
_ZOOM_LEVELS = (1, 2, 4, 8, 16)

# 行尾操作：preview 不作为胶囊（预览由选中行承担）；detail 每行恒有。
ACTION_LABELS = {
    "detail": "详情", "edit_pos": "改位置", "edit_wait": "改等待",
    "delete": "删除", "expand": "展开", "collapse": "折叠",
}
_ACTION_ORDER = ("detail", "edit_pos", "edit_wait", "delete", "expand", "collapse")


def row_action_chips(capability):
    """能力集合 → 行尾胶囊（固定顺序，detail 恒在首位）。"""
    caps = set(capability or ())
    return tuple((action, ACTION_LABELS[action]) for action in _ACTION_ORDER if action in caps)


def chip_metrics(view, chips):
    """胶囊宽度唯一由 CT_PushButton 度量给出，不写像素常量。"""
    style = view.style()
    metrics = view.fontMetrics()
    option = QStyleOptionButton()
    option.state = QStyle.State_Enabled
    widths = []
    height = 0
    for _action, label in chips:
        option.text = label
        # DESIGN: 内容高度须为字体实际行高。传 0 时浅色主题 QSS 的按钮只剩边框，
        # 高度退化到约 2px（细线），绘制与点击命中区都不可用；高度由主题/缩放决定，
        # 不写像素常量。
        size = style.sizeFromContents(
            QStyle.CT_PushButton, option,
            QSize(metrics.horizontalAdvance(label) + 12, metrics.height()), view
        )
        widths.append(max(1, size.width()))
        height = max(height, size.height())
    return tuple(widths), height


def chip_rects(row_rect, chips, view):
    """绘制与命中共用的胶囊矩形；返回 ((action, QRect), ...)。"""
    widths, height = chip_metrics(view, chips)
    gap = 4
    total = sum(widths) + gap * max(0, len(widths) - 1)
    x = max(row_rect.left(), row_rect.right() - total)
    y = row_rect.top() + max(0, (row_rect.height() - height) // 2)
    result = []
    for (action, _label), width in zip(chips, widths, strict=True):
        result.append((action, QRect(x, y, width, height)))
        x += width + gap
    return tuple(result)


def plan_column_widths(viewport, desired, minimums, scrollbar_extent=0):
    """按列序返回 (动作, 参数, 时间, 操作) 四列宽度与 needs_hscroll。"""
    min_action = minimums["action"]
    min_param = minimums["param"]
    min_time = minimums["time"]
    min_op = minimums["op"]
    # DESIGN: 时间列在横向滚动时也保留完整单元格宽度。
    time_floor = max(min_time, desired["time"])
    act = max(min_op, min(desired["op"], int(viewport * 0.30)))
    budget = viewport - act - scrollbar_extent
    if budget < time_floor + min_action + min_param:
        return (min_action, min_param, time_floor, min_op), True
    time = time_floor
    rest = budget - time
    action_col = max(min_action, min(desired["action"], rest - min_param))
    param = rest - action_col
    return (action_col, param, time, act), False


class _ActionDelegate(QStyledItemDelegate):
    """操作列代理：绘制胶囊并处理命中，不创建逐行控件。"""

    actionTriggered = Signal(str, str)

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.hover = None
        self.pressed = None

    def _chips(self, index):
        return tuple(index.data(Qt.UserRole + 1) or ())

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        chips = self._chips(index)
        row_id = str(index.siblingAtColumn(0).data(Qt.UserRole) or "")
        for action, rect in chip_rects(option.rect, chips, self.view):
            opt = QStyleOptionButton()
            opt.rect = rect
            opt.text = dict(chips).get(action, action)
            opt.state = QStyle.State_Enabled
            if self.hover == (row_id, action):
                opt.state |= QStyle.State_MouseOver
            if self.pressed == (row_id, action):
                opt.state |= QStyle.State_Sunken
            opt.palette = option.palette
            self.view.style().drawControl(QStyle.CE_PushButton, opt, painter, self.view)

    def sizeHint(self, option, index):
        widths, height = chip_metrics(self.view, self._chips(index))
        return QSize(sum(widths) + 4 * max(0, len(widths) - 1) + 8, height + 8)

    def editorEvent(self, event, model, option, index):
        row_id = str(index.siblingAtColumn(0).data(Qt.UserRole) or "")
        hit = None
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            point = event.position().toPoint() if hasattr(event, "position") else event.pos()
            for action, rect in chip_rects(option.rect, self._chips(index), self.view):
                if rect.contains(point):
                    hit = action
                    break
        if event.type() == QEvent.MouseButtonPress and hit:
            self.pressed = (row_id, hit)
            self.view.setCurrentIndex(index)
            return True
        if event.type() == QEvent.MouseButtonRelease:
            if self.pressed and self.pressed[0] == row_id and hit == self.pressed[1]:
                self.actionTriggered.emit(row_id, hit)
            self.pressed = None
            return hit is not None
        return False

    def helpEvent(self, event, view, option, index):
        if isinstance(event, QHelpEvent):
            point = event.pos()
            for action, rect in chip_rects(option.rect, self._chips(index), self.view):
                if rect.contains(point):
                    label = dict(self._chips(index)).get(action, action)
                    tip = "在下方展开/收起详情" if action == "detail" else label
                    view.setToolTip(tip)
                    return True
        return super().helpEvent(event, view, option, index)


class _TimelineTree(QTreeWidget):
    """树控件：把右键与鼠标移动交给编辑面板，避免创建逐行控件。"""

    contextRequested = Signal(object)
    hoverMoved = Signal(object)

    def contextMenuEvent(self, event):
        self.contextRequested.emit(event.pos())
        event.accept()

    def mouseMoveEvent(self, event):
        point = event.position().toPoint() if hasattr(event, "position") else event.pos()
        self.hoverMoved.emit(point)
        super().mouseMoveEvent(event)


class _TimelineTrack(QWidget):
    """时间线：按可见时间范围绘制动作段与移动点。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events: list[dict] = []
        self.groups: list[ActionGroup] = []
        self.window_start = 0.0
        self.window_end = 1.0
        self.cursor_t = 0.0
        self.selected_id = ""

    def sizeHint(self):
        return QSize(0, self.fontMetrics().lineSpacing() * 3)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if not self.events or self.window_end <= self.window_start:
            return
        width = max(1, self.width())
        height = max(1, self.height())

        def x_for(moment):
            ratio = (moment - self.window_start) / (self.window_end - self.window_start)
            return int(ratio * width)

        # 轨道基线
        painter.setPen(QPen(_PREVIEW_MUTED, 1))
        mid = height // 2
        painter.drawLine(0, mid, width, mid)

        for group in self.groups:
            if group.t_end < self.window_start or group.t_start > self.window_end:
                continue
            x0 = max(0, min(width, x_for(group.t_start)))
            x1 = max(0, min(width, x_for(group.t_end)))
            active = group.id == self.selected_id
            painter.setPen(QPen(_PREVIEW_ACTIVE if active else _PREVIEW_LINE, 3 if active else 1))
            if group.kind == "move":
                painter.drawPoint(x0, mid)
            else:
                painter.drawLine(x0, mid, max(x0, x1), mid)

        # 虚拟光标
        if self.window_start <= self.cursor_t <= self.window_end:
            cx = x_for(self.cursor_t)
            painter.setPen(QPen(_PREVIEW_CURSOR, 2))
            painter.drawLine(cx, 0, cx, height)


class _PreviewCanvas(QWidget):
    """轨迹预览：绘制移动点连线与虚拟光标落点。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events: list[dict] = []
        self.cursor_t = 0.0
        self.window_start = 0.0
        self.window_end = 1.0
        self.scope_indices: frozenset[int] = frozenset()

    def sizeHint(self):
        return QSize(0, self.fontMetrics().lineSpacing() * 5)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        moves = [
            (index, e)
            for index, e in enumerate(self.events)
            if e.get("kind") == "move"
            and self.window_start <= float(e.get("t", 0.0)) <= self.window_end
            and isinstance(e.get("x"), int)
            and isinstance(e.get("y"), int)
            and (not self.scope_indices or index in self.scope_indices)
        ]
        if not moves:
            return
        xs = [e["x"] for _index, e in moves]
        ys = [e["y"] for _index, e in moves]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(1, max_x - min_x)
        span_y = max(1, max_y - min_y)
        w, h = max(1, self.width() - 2), max(1, self.height() - 2)

        def pos(item):
            _index, e = item
            return QPoint(
                1 + int((e["x"] - min_x) / span_x * w),
                1 + int((e["y"] - min_y) / span_y * h),
            )

        painter.setPen(QPen(_PREVIEW_LINE, 2))
        points = [pos(item) for item in moves]
        for a, b in zip(points, points[1:], strict=False):
            painter.drawLine(a, b)
        painter.setPen(QPen(_PREVIEW_CURSOR, 4))
        for point in points:
            painter.drawPoint(point)

        # 虚拟光标：光标时刻之前最后一个移动点
        before = [
            item
            for item in moves
            if float(item[1].get("t", 0.0)) <= self.cursor_t + 1e-9
        ]
        if before:
            cursor = pos(before[-1])
            painter.setPen(QPen(_PREVIEW_ACTIVE, 2))
            painter.drawRect(QRect(cursor.x() - 3, cursor.y() - 3, 6, 6))


def wait_gap_choices(events):
    """可调整的空等区间及显示文案，含开头空等（boundary=0）。

    任务书首版明确支持开头空等；模型 adjust_wait(boundary=0) 已支持，
    界面不得再以 boundary > 0 过滤掉它。
    """
    result = []
    for gap in find_wait_gaps(events):
        if not gap.editable:
            continue
        label = (
            f"开头空等：{gap.duration:.3f}s"
            if gap.boundary == 0
            else f"事件 {gap.boundary} 前：{gap.duration:.3f}s"
        )
        result.append((gap, label))
    return result


class TimelineEditorPanel(QWidget):
    """工作流步骤内的录制草稿与编辑控件。"""

    applyRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.setObjectName("timelineEditorPanel")
        # 编辑器内的鼠标操作不能冒泡成外层步骤的拖动。
        self.setAttribute(Qt.WA_NoMousePropagation)
        self._source_label = timeline_source_label(task)
        raw = task.get("input_events") if isinstance(task, dict) else None
        self._original_events = copy.deepcopy(raw) if isinstance(raw, list) else []
        self._events: list[dict] = copy.deepcopy(self._original_events)
        self._undo_stack: list[list[dict]] = []
        self._groups: list[ActionGroup] = []
        self._display_rows: list[DisplayRow] = []
        self._expanded_ids: set[str] = set()
        self._selected_id = ""
        self._zoom_index = 0
        self._visible_start = 0.0
        self._duration = 0.0
        self._build_ui()
        self._reload()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # 顶部紧凑信息条：来源、时长/动作数与现有「调整等待…」入口同一行，
        # 高度随内容，不给主体区域制造纵向留白。
        header = QHBoxLayout()
        header.addWidget(QLabel(f"{self._source_label}录制"))
        header.addWidget(
            HelpBtn(
                "修改先保存在草稿中，点击「应用」才写入当前步骤，点击「取消」丢弃草稿。"
                "每行行尾按钮可查看详情、改位置、改等待、删除、展开或折叠。"
                "点击「详情」在列表下方展开时间线与轨迹（拖拽与移动仅可预览），再次点击收起。"
            )
        )
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("timelineSummary")
        header.addWidget(self.summary_label)
        header.addStretch(1)
        self.edit_wait_btn = QPushButton("调整等待…")
        self.edit_wait_btn.setObjectName("timelineEditWait")
        self.edit_wait_btn.clicked.connect(self._on_edit_wait)
        header.addWidget(self.edit_wait_btn)
        root.addLayout(header)

        # 主视图：四列步骤列表整宽占满剩余高度与宽度。列表自身继承剩余空间，
        # 不放在横向 splitter 里被右侧面板挤压（原布局选中后左侧仅约 498px）。
        self.event_list = _TimelineTree()
        self.event_list.setObjectName("timelineEventList")
        self.event_list.setColumnCount(4)
        self.event_list.setHeaderLabels(["动作", "参数", "时间", "操作"])
        self.event_list.header().setStretchLastSection(False)
        self.event_list.setRootIsDecorated(False)
        self.event_list.setUniformRowHeights(True)
        self.event_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.event_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.event_list.setMouseTracking(True)
        self.event_list.currentItemChanged.connect(self._on_row_changed)
        self.event_list.contextRequested.connect(self._on_context_menu)
        self.event_list.hoverMoved.connect(self._on_hover)
        self.action_delegate = _ActionDelegate(self.event_list)
        self.action_delegate.actionTriggered.connect(self._on_row_action)
        self.event_list.setItemDelegateForColumn(3, self.action_delegate)
        self.event_list.verticalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self, self._apply_column_widths)
        )
        self._restoring_selection = False

        # 详情区位于列表下方、与列表同宽，默认收起；点击「详情」才展开。
        # 列表与详情放进同一个纵向 splitter：详情收起时 splitter 只剩列表，
        # 列表自动占满主区域；展开时两者仍整宽，且各自保有可滚动区。
        self.detail_splitter = QSplitter(Qt.Vertical)
        self.detail_splitter.setObjectName("timelineDetailSplitter")
        self.detail_splitter.setChildrenCollapsible(False)
        self.detail_splitter.addWidget(self.event_list)

        self.detail_panel = QWidget()
        self.detail_panel.setObjectName("timelineDetailPanel")
        right_layout = QVBoxLayout(self.detail_panel)
        right_layout.addWidget(QLabel("详情 · 时间线 / 轨迹预览 / 原始事件"))
        self.track = _TimelineTrack()
        self.track.setObjectName("timelineTrack")
        right_layout.addWidget(self.track)

        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.setObjectName("timelineScroll")
        self.scroll.valueChanged.connect(self._on_scroll)
        right_layout.addWidget(self.scroll)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("缩放"))
        self.zoom = QComboBox()
        self.zoom.setObjectName("timelineZoom")
        for level in _ZOOM_LEVELS:
            self.zoom.addItem(f"{level}x", level)
        self.zoom.currentIndexChanged.connect(self._on_zoom)
        controls.addWidget(self.zoom)
        controls.addWidget(QLabel("游标时刻（秒）"))
        self.cursor_spin = QDoubleSpinBox()
        self.cursor_spin.setObjectName("timelineCursor")
        self.cursor_spin.setDecimals(3)
        self.cursor_spin.setMaximum(86_400.0)
        self.cursor_spin.valueChanged.connect(self._on_cursor)
        controls.addWidget(self.cursor_spin)
        controls.addStretch(1)
        right_layout.addLayout(controls)

        right_layout.addWidget(QLabel("轨迹预览（逻辑坐标）"))
        self.preview = _PreviewCanvas()
        self.preview.setObjectName("timelinePreview")
        right_layout.addWidget(self.preview)
        self.detail = QLabel("选择一个动作查看起止时刻与坐标")
        self.detail.setObjectName("timelineDetail")
        self.detail.setWordWrap(True)
        right_layout.addWidget(self.detail)
        self.detail_splitter.addWidget(self.detail_panel)
        # 详情默认收起；列表随主体伸缩，详情区取自身所需高度。
        self.detail_splitter.setStretchFactor(0, 1)
        self.detail_splitter.setStretchFactor(1, 0)
        self.detail_panel.setVisible(False)
        root.addWidget(self.detail_splitter, 1)

        footer = QHBoxLayout()
        self.undo_btn = QPushButton("撤销")
        self.undo_btn.setObjectName("timelineUndo")
        self.undo_btn.clicked.connect(self._on_undo)
        footer.addWidget(self.undo_btn)
        self.status = QLabel("")
        self.status.setObjectName("timelineStatus")
        self.status.setWordWrap(True)
        footer.addWidget(self.status)
        footer.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("timelineCancel")
        cancel.clicked.connect(self.cancelRequested)
        apply_btn = QPushButton("应用")
        apply_btn.setObjectName("timelineApply")
        apply_btn.clicked.connect(self._on_apply)
        footer.addWidget(cancel)
        footer.addWidget(apply_btn)
        root.addLayout(footer)

    # ---- 数据加载与可见范围 ----
    def _reload(self):
        self._groups = group_events(self._events)
        self._duration = (
            max(0.0, float(self._events[-1]["t"])) if self._events else 0.0
        )
        self.summary_label.setText(
            f"时长 {self._duration:.2f} 秒 · {len(self._groups)} 个动作"
        )
        self.cursor_spin.blockSignals(True)
        self.cursor_spin.setMaximum(max(1.0, self._duration))
        self.cursor_spin.blockSignals(False)
        self._refresh_visible_range()
        self._load_rows()
        self._refresh_canvas()

    def _refresh_visible_range(self):
        zoom = int(self.zoom.currentData() or 1)
        self._visible_start = min(
            max(0.0, self._visible_start), max(0.0, self._duration - self._duration / zoom)
        )
        self._visible_end = self._visible_start + max(
            0.001, self._duration / zoom if self._duration else 1.0
        )
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, max(0, int((self._duration - self._visible_end) * 1000)))
        self.scroll.blockSignals(False)
        self.track.window_start = self._visible_start
        self.track.window_end = self._visible_end
        self.preview.window_start = self._visible_start
        self.preview.window_end = self._visible_end

    def _visible_rows(self):
        return [
            row for row in self._display_rows
            if row.t_end >= self._visible_start and row.t_start <= self._visible_end
        ]

    @staticmethod
    def _row_signature(rows):
        return tuple(
            (row.id, row.label, row.params, row.capability, row.t_start, row.t_end, row.reason)
            for row in rows
        )

    def _reference_minimums(self):
        metrics = self.event_list.fontMetrics()

        def width(text):
            return metrics.horizontalAdvance(text) + 12

        chips = tuple((action, ACTION_LABELS[action]) for action in ("detail", "edit_pos", "delete"))
        chip_widths, _height = chip_metrics(self.event_list, chips)
        return {
            "action": width("000. 连续移动（00 点）"),
            "param": width("(100, 100)"),
            "time": width("0.000–0.000 秒"),
            "op": sum(chip_widths) + 8,
        }

    def _apply_column_widths(self):
        visible = self._visible_rows()
        tree = self.event_list
        row_count = tree.topLevelItemCount()
        if not visible or not row_count:
            return
        metrics = tree.fontMetrics()

        def desired_width(texts):
            return max((metrics.horizontalAdvance(t) for t in texts), default=0) + 12

        minimums = self._reference_minimums()
        chips = tuple((action, ACTION_LABELS[action]) for action in ("detail", "edit_pos", "delete"))
        chip_widths, _height = chip_metrics(self.event_list, chips)
        # INVARIANT: 时间单元格共用字体和代理。找到最宽文字后，只测一次实际 Qt 单元格。
        widest_time_row = max(
            range(row_count), key=lambda row: metrics.horizontalAdvance(tree.topLevelItem(row).text(2))
        )
        time_index = tree.model().index(widest_time_row, 2)
        desired = {
            "action": desired_width([f"{i:>3}. {row.label}" for i, row in enumerate(visible, 1)]),
            "param": desired_width([row.params for row in visible]),
            "time": tree.sizeHintForIndex(time_index).width(),
            "op": sum(chip_widths) + 8,
        }
        extent = tree.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        widths, _hscroll = plan_column_widths(tree.viewport().width(), desired, minimums, extent)
        for column, width in enumerate(widths):
            tree.setColumnWidth(column, width)

    def _load_rows(self, *, force=True):
        """按可见时间范围加载四列步骤行；滚动/缩放时按内容签名跳过无变化重建。"""
        self._display_rows = build_display_rows(
            self._events, expanded_ids=frozenset(self._expanded_ids)
        )
        visible = self._visible_rows()
        signature = self._row_signature(visible)
        if not force and signature == getattr(self, "_visible_signature", None):
            return
        self._visible_signature = signature
        wanted = self._selected_id
        self.event_list.blockSignals(True)
        self.event_list.clear()
        for order, row in enumerate(visible, start=1):
            time_text = f"{row.t_start:.3f}"
            if row.t_start != row.t_end:
                time_text += f"–{row.t_end:.3f}"
            item = QTreeWidgetItem([
                f"{order:>3}. {row.label}",
                row.params,
                f"{time_text} 秒",
                "",
            ])
            item.setData(0, Qt.UserRole, row.id)
            item.setData(3, Qt.UserRole + 1, row_action_chips(row.capability))
            if row.reason:
                item.setToolTip(0, row.reason)
            self.event_list.addTopLevelItem(item)
        self.event_list.blockSignals(False)
        self._restoring_selection = True
        try:
            ids = {row.id for row in visible}
            if wanted in ids:
                item = next(
                    (self.event_list.topLevelItem(i) for i in range(self.event_list.topLevelItemCount())
                     if self.event_list.topLevelItem(i).data(0, Qt.UserRole) == wanted),
                    None,
                )
                if item is not None:
                    self.event_list.setCurrentItem(item)
                    self.event_list.scrollToItem(item)
                self._selected_id = wanted
                # 重建期 currentItemChanged 被守卫跳过，这里按最终 selection 补刷详情，
                # 使列表/画布/详情指向同一行（删除后 id 复用时尤其必要）。
                self._render_detail(self._selected_row())
            else:
                self._selected_id = ""
                self.detail.setText("选择一个动作查看起止时刻与坐标")
                self.track.selected_id = ""
                self.preview.scope_indices = frozenset()
        finally:
            self._restoring_selection = False
        self.edit_wait_btn.setEnabled(bool(wait_gap_choices(self._events)))
        self._apply_column_widths()
        QTimer.singleShot(0, self, self._apply_column_widths)
        self._refresh_canvas()

    def _refresh_canvas(self):
        cursor = float(self.cursor_spin.value())
        selected = self._selected_row()
        self.track.events = self._events
        self.track.groups = self._groups
        self.track.cursor_t = cursor
        self.track.selected_id = selected.group_ids[0] if selected and selected.group_ids else ""
        self.preview.events = self._events
        self.preview.cursor_t = cursor
        self.preview.scope_indices = frozenset(selected.indices) if selected else frozenset()
        self.track.update()
        self.preview.update()

    # ---- 交互 ----
    def _on_scroll(self, value):
        self._visible_start = value / 1000.0
        self._refresh_visible_range()
        self._load_rows(force=False)
        self._refresh_canvas()

    def _on_zoom(self):
        self._refresh_visible_range()
        self._load_rows(force=False)
        self._refresh_canvas()

    def _on_cursor(self, value):
        self._refresh_canvas()

    def _selected_row(self):
        return next((row for row in self._display_rows if row.id == self._selected_id), None)

    def _render_detail(self, selected):
        """只更新待查看内容，不发射信号、不刷新画布、不自动展开详情区。"""
        if selected is None:
            self.detail.setText("选择一个动作查看起止时刻与坐标")
            return
        extra = f"；{selected.reason}" if selected.reason else ""
        raw_text = "; ".join(
            str(self._events[idx]) for idx in selected.indices[:8] if idx < len(self._events)
        )
        self.detail.setText(
            f"{selected.label}：{selected.t_start:.3f}s → {selected.t_end:.3f}s，"
            f"{selected.params}{extra}"
            + (f"\n原始事件：{raw_text}" if raw_text else "")
        )
        # DESIGN: 详情默认收起，只有点击「详情」胶囊/右键项才在列表下方展开。
        # 选中行只更新待查看内容；已展开时改选其他行则内容随选中行同步刷新。

    def _on_row_changed(self, current, previous=None):
        if getattr(self, "_restoring_selection", False):
            return
        row_id = current.data(0, Qt.UserRole) if current is not None else ""
        self._selected_id = str(row_id or "")
        self._render_detail(self._selected_row())
        self._refresh_canvas()

    def _item_by_id(self, row_id):
        return next(
            (self.event_list.topLevelItem(i) for i in range(self.event_list.topLevelItemCount())
             if self.event_list.topLevelItem(i).data(0, Qt.UserRole) == row_id),
            None,
        )

    def _row_by_id(self, row_id):
        return next((row for row in self._display_rows if row.id == row_id), None)

    def _on_row_action(self, row_id, action):
        """行尾胶囊动作：按 row_id 定位，绝不读 _selected_id。"""
        if action == "detail":
            item = self._item_by_id(row_id)
            if item is not None:
                self.event_list.setCurrentItem(item)
            # 详情默认收起；点击「详情」在列表下方展开，再次点击收起释放高度。
            # DESIGN: 列表始终是主视图，详情按需出现，避免选中行挤窄列表。
            self.detail_panel.setVisible(self.detail_panel.isHidden())
            self._render_detail(self._selected_row())
            # 分隔器的内容高度已改变，通知外层列表重新计算整个步骤的高度。
            self.detail_splitter.updateGeometry()
            return
        if action == "edit_pos":
            self._on_edit_position(row_id)
        elif action == "edit_wait":
            row = self._row_by_id(row_id)
            self._on_edit_wait(boundary=row.wait_boundary if row else None)
        elif action == "delete":
            self._on_delete(row_id)
        elif action in ("expand", "collapse"):
            self._on_toggle_expand(row_id)

    def _on_hover(self, point):
        viewport_point = point  # hoverMoved 已发视口坐标，不得再做坐标变换
        index = self.event_list.indexAt(viewport_point)
        row_id = str(index.siblingAtColumn(0).data(Qt.UserRole) or "") if index.isValid() else ""
        target = None
        if index.isValid():
            for action, rect in chip_rects(
                self.event_list.visualRect(index),
                tuple(index.model().index(index.row(), 3).data(Qt.UserRole + 1) or ()),
                self.event_list,
            ):
                if rect.contains(viewport_point):
                    target = (row_id, action)
                    break
        if target == getattr(self.action_delegate, "hover", None):
            return
        previous = getattr(self.action_delegate, "hover", None)
        self.action_delegate.hover = target
        for stale in (previous, target):
            if stale is not None:
                item = self._item_by_id(stale[0])
                if item is not None:
                    cell = self.event_list.model().index(self.event_list.indexOfTopLevelItem(item), 3)
                    self.event_list.update(cell)

    def _on_context_menu(self, point):
        # contextRequested 与 hoverMoved 一样发的是视口坐标，indexAt 直接接受；
        # 不得再做 mapFrom/mapTo 变换，否则 indexAt 命中上一行。
        index = self.event_list.indexAt(point)
        if not index.isValid():
            return
        row_id = str(index.siblingAtColumn(0).data(Qt.UserRole) or "")
        row = self._row_by_id(row_id)
        if row is None:
            return
        menu = QMenu(self)
        for action, label in row_action_chips(row.capability):
            item = menu.addAction("在下方展开/收起详情" if action == "detail" else label)
            item.setEnabled(True)
            item.triggered.connect(
                lambda checked=False, rid=row_id, act=action: self._on_row_action(rid, act)
            )
        menu.exec(self.event_list.viewport().mapToGlobal(point))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            row_id = self._selected_id
            row = self._row_by_id(row_id)
            if row is not None and "delete" in row.capability:
                self._on_delete(row_id)
            else:
                self.status.setText("当前行不能删除")
        elif event.matches(QKeySequence.Undo):
            self._on_undo()
        elif event.key() == Qt.Key_Escape:
            self.cancelRequested.emit()
        # SAFETY: 面板内的编辑键不能触发主列表的删除、剪切或撤销。
        event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_column_widths()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_column_widths()

    def event(self, event):
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            QTimer.singleShot(0, self, self._apply_column_widths)
        return super().event(event)

    def _on_toggle_expand(self, row_id=None):
        row = self._row_by_id(row_id or self._selected_id)
        if row is None or not (row.expandable or row.expanded):
            self.status.setText("该行没有可展开的折叠内容")
            return
        fold_id = row.parent_id or row.id
        if not row.parent_id and row.id.startswith("run_"):
            if fold_id in self._expanded_ids:
                self._expanded_ids.discard(fold_id)
            else:
                self._expanded_ids.add(fold_id)
        else:
            self.status.setText("请选中折叠行进行展开或折叠")
            return
        self._reload()
        self.status.setText("已更新折叠显示，事件未改变")

    def _commit(self, result):
        if not result.ok:
            self.status.setText(f"未应用：{result.reason}")
            return False
        self._undo_stack.append(copy.deepcopy(self._events))
        self._events = result.events
        self.status.setText("已修改副本，可继续编辑或撤销")
        self._reload()
        return True

    def _on_edit_position(self, row_id=None):
        from PySide6.QtWidgets import QInputDialog

        group = self._selected_group(row_id)
        if group is None or not group.editable_click:
            self.status.setText(
                "仅支持起落点一致、按住期间无拖拽、定位关系明确的单击"
            )
            return
        x, ok_x = QInputDialog.getInt(self, "修改单击位置", "X 坐标", group.x or 0, -100_000, 100_000)
        if not ok_x or not widget_is_alive(self) or not self.isEnabled():
            return
        y, ok_y = QInputDialog.getInt(self, "修改单击位置", "Y 坐标", group.y or 0, -100_000, 100_000)
        if not ok_y or not widget_is_alive(self) or not self.isEnabled():
            return
        self._commit(edit_click_position(self._events, group.id, x, y))

    def _on_edit_wait(self, *, boundary=None, row_id=None):
        from PySide6.QtWidgets import QInputDialog

        choices = wait_gap_choices(self._events)
        if not choices:
            self.status.setText("没有可调整的空等区间")
            return
        labels = [label for _gap, label in choices]
        initial = next((i for i, (gap, _label) in enumerate(choices) if gap.boundary == boundary), 0)
        text, ok = QInputDialog.getItem(self, "调整等待", "选择区间", labels, initial, False)
        if not ok or not widget_is_alive(self) or not self.isEnabled():
            return
        gap = choices[labels.index(text)][0]
        delta, ok = QInputDialog.getDouble(
            self, "调整等待", "变化（秒，负数缩短）", 0.0, -86_400.0, 86_400.0, 3
        )
        if not ok or not widget_is_alive(self) or not self.isEnabled():
            return
        self._commit(adjust_wait(self._events, gap.boundary, delta))

    def _selected_group(self, row_id=None):
        row = self._row_by_id(row_id or self._selected_id)
        if row is None or not row.group_ids:
            return None
        return next((g for g in self._groups if g.id == row.group_ids[0]), None)

    def _on_delete(self, row_id=None):
        row = self._row_by_id(row_id or self._selected_id)
        if row is None or not row.group_ids:
            self.status.setText("请先选择一个可删除的动作")
            return
        self._commit(delete_action(self._events, row.group_ids[0]))

    def _on_undo(self):
        if not self._undo_stack:
            self.status.setText("没有可撤销的修改")
            return
        self._events = self._undo_stack.pop()
        self.status.setText("已撤销上一步")
        self._reload()

    def _on_apply(self):
        if not self.isEnabled():
            return
        errors = validate_edited_timeline(self._events)
        if errors:
            self.status.setText(f"时序无效，未应用：{errors[0]}")
            return
        self.applyRequested.emit(self.edited_events())

    def edited_events(self):
        return copy.deepcopy(self._events)

    def has_changes(self):
        return self._events != self._original_events
