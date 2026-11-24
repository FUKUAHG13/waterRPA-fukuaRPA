# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false
"""主步骤列表滚动期的同版式轻量预览委托。

拖动右侧滚动条或普通滚动尚未来得及物化控件时，未物化的空行由本委托按
``TaskRow`` 的正常版式绘制（编号/动作/参数分列 + 分组标题），避免旧版纯文本
摘要占位与真实控件行同屏混杂。松手补齐后该行有了 itemWidget，委托自动让位，
保证预览到可编辑控件连续可见。

契约（见实施计划 3.2）：
1. 只对「未物化 + 有步骤数据 + text 为空 + 非手动拖拽」的行绘制；其余无条件 super().paint()。
2. 不覆写 sizeHint()（沿用 item 的 sizeHint 路径）。
3. paint() 纯只读：不写 item text/data/sizeHint，不调度刷新。
4. 颜色取自 palette / theme token，字体取自视图，度量取自 task_row_metrics。
"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QStyledItemDelegate

from .. import task_row_metrics as metrics

# 缓存上限：防止长时间滚动时预览字段缓存无界增长
_FIELD_CACHE_LIMIT = 256


class StepRowPreviewDelegate(QStyledItemDelegate):
    """未物化步骤行的同版式预览绘制委托（只读）。"""

    def __init__(self, view):
        super().__init__(view)
        self._view = view
        # key: (row, id(item)) -> (id(userdata), fields)；带上限，随视图销毁
        self._field_cache = {}
        self._cache_key = None

    # ------------------------------------------------------------------
    # 判定与数据（供测试复用）
    # ------------------------------------------------------------------
    def should_preview(self, index):
        """是否应对该 index 绘制预览（纯只读判定）。"""
        if not index.isValid():
            return False
        view = self._view
        row = index.row()
        item = view.item(row)
        if item is None or item.isHidden():
            return False
        # 手动拖拽摘要占位优先，委托让位
        if getattr(view, "_drag_active", False):
            return False
        # 已有控件的行由 widget 绘制
        if view.itemWidget(item) is not None:
            return False
        # 有文本（手动拖拽摘要/其它）时让位
        if item.text():
            return False
        # 必须有结构化步骤数据
        if not isinstance(item.data(Qt.UserRole), dict):
            return False
        # 仅虚拟化列表需要预览（<= 阈值时行均已物化，此判定也避免无谓开销）
        active = getattr(view, "virtualization_active", None)
        return not (callable(active) and not active())

    def preview_fields(self, index):
        """返回绘制所需字段，按 item 数据身份缓存。"""
        view = self._view
        row = index.row()
        item = view.item(row)
        data = item.data(Qt.UserRole)
        # INVARIANT：缓存 key 必须含选中态。ExtendedSelection 下选中未物化预览行
        # 时 data 对象不变，若 key 只含 id(data)，selected 字段陈旧、背景不刷新。
        key = (row, id(item), bool(item.isSelected()))
        data_id = id(data)
        cached = self._field_cache.get(key)
        if cached is not None and cached[0] == data_id:
            return cached[1]

        fields = self._build_fields(row, item, data)
        if len(self._field_cache) >= _FIELD_CACHE_LIMIT:
            self._field_cache.clear()
        self._field_cache[key] = (data_id, fields)
        return fields

    def _build_fields(self, row, item, data):
        presentation = item.data(Qt.UserRole + 2)
        presentation = presentation if isinstance(presentation, dict) else {}
        headers = presentation.get("headers", [])
        headers = [h for h in headers if isinstance(h, dict)][:3]  # 最多 3 层，与真实行 3 frame 一致
        depth = max(0, min(3, int(presentation.get("depth", 0))))

        owner = self._view.window()
        settings_mode = "simple"
        show_note = False
        if owner is not None:
            getter = getattr(owner, "current_settings_mode", None)
            if callable(getter):
                settings_mode = str(getter())
            show_note = bool(getattr(owner, "show_step_notes", False))

        from ...workflow_summary import task_drag_summary

        summary = str(item.data(Qt.UserRole + 1) or "").strip()
        if not summary:
            summary = task_drag_summary(data, row + 1, settings_mode)

        # 从结构化数据拆列；专用摘要（直到条件/多图）沿用 task_drag_summary 文本，
        # 按 " | " 拆分动作与参数，保证与真实行 drag_summary 同一展示口径。
        type_label, value = _split_summary(summary)
        marks = _summary_marks(summary)
        return {
            "row": row,
            "index": f"{row + 1}.",
            "type": type_label,
            "value": value,
            "marks": marks,
            "headers": headers,
            "depth": depth,
            "controls_hidden": bool(presentation.get("controls_hidden", False)),
            "group_start": bool(presentation.get("group_start", False)),
            "group_end": bool(presentation.get("group_end", False)),
            "selected": bool(item.isSelected()),
            "settings_mode": settings_mode,
            "show_note": show_note,
            "maximum_index": max(1, self._view.count()),
        }

    def invalidate(self):
        """清空缓存（切换方案/缩放/关窗时由宿主调用）；无副作用。"""
        self._field_cache.clear()
        self._cache_key = None

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paint(self, painter, option, index):
        if not self.should_preview(index):
            super().paint(painter, option, index)
            return
        fields = self.preview_fields(index)
        self.draw_preview(painter, option, fields)

    def preview_rects(self, option, fields):
        """供测试断言的列矩形（与 paint 同源，不重复布局逻辑）。"""
        rect = option.rect
        fm = QFontMetrics(self._view.font())
        header_h = self._header_height()
        headers = fields["headers"]
        header_rects = []
        top = rect.top()
        for header in headers:
            hrect = QRect(rect.left(), top, rect.width(), header_h)
            header_rects.append(
                {
                    "rect": hrect,
                    "layout": metrics.header_layout(
                        hrect,
                        int(header.get("depth", 1)),
                        title_width=max(
                            1,
                            fm.horizontalAdvance(str(header.get("name", ""))),
                        ),
                        count_width=max(
                            1,
                            fm.horizontalAdvance(
                                f"{max(1, int(header.get('count', 1)))} 步"
                            ),
                        ),
                    ),
                    "header": header,
                }
            )
            top += header_h

        body_top = top
        body_h = rect.bottom() - body_top
        body = QRect(rect.left(), body_top, rect.width(), max(0, body_h))
        cols = metrics.content_columns(
            body,
            depth=fields["depth"],
            maximum_index=fields["maximum_index"],
            metrics=fm,
            mark_count=len(fields["marks"]),
        )
        return {"headers": header_rects, "body": body, "columns": cols}

    def _header_height(self):
        height = getattr(self._view, "_virtual_group_header_height", 32)
        return max(1, int(height))

    def draw_preview(self, painter, option, fields):
        rects = self.preview_rects(option, fields)
        painter.save()
        try:
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            self._draw_background(painter, option, fields)
            for hinfo in rects["headers"]:
                self._draw_header(painter, hinfo, option)
            if not fields["controls_hidden"]:
                self._draw_body(painter, option, fields, rects)
        finally:
            painter.restore()

    def _draw_background(self, painter, option, fields):
        rect = option.rect
        palette = option.palette
        # 正文底色：选中态用 Highlight，普通态用 Base；边框用 palette 派生
        if fields["selected"]:
            painter.fillRect(rect, palette.color(QPalette.Highlight))
        else:
            painter.fillRect(rect, palette.color(QPalette.Base))
        border = QColor(palette.color(QPalette.Mid))
        border.setAlpha(160)
        painter.setPen(border)
        painter.drawRect(
            rect.adjusted(0, 0, -1, -1)
        )  # QRect 边界内含，避免相邻行重叠

    def _draw_header(self, painter, hinfo, option):
        header = hinfo["header"]
        layout = hinfo["layout"]
        rect = hinfo["rect"]
        palette = option.palette
        depth = max(1, min(3, int(header.get("depth", 1))))

        header_bg = QColor(palette.color(QPalette.AlternateBase))
        if depth == 1:
            header_bg = QColor(palette.color(QPalette.Highlight))
            header_bg.setAlpha(70)
        painter.fillRect(rect, header_bg)

        fm = QFontMetrics(self._view.font())
        collapsed = bool(header.get("collapsed", False))
        arrow = "▸" if collapsed else "▾"
        toggle_rect = layout["toggle"]
        painter.setPen(QColor(palette.color(QPalette.Highlight)))
        painter.drawText(toggle_rect, Qt.AlignCenter, arrow)

        title_font = QFont(self._view.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(
            palette.color(QPalette.HighlightedText) if depth == 1
            else palette.color(QPalette.WindowText)
        ))
        title = metrics.elide(fm, str(header.get("name", "未命名分组")),
                              layout["title"].width())
        painter.drawText(layout["title"], Qt.AlignVCenter | Qt.AlignLeft, title)

        muted = QColor(palette.color(QPalette.WindowText))
        muted.setAlpha(170)
        painter.setPen(muted)
        count_text = f"{max(1, int(header.get('count', 1)))} 步"
        painter.setFont(self._view.font())
        painter.drawText(layout["count"], Qt.AlignVCenter | Qt.AlignRight,
                         metrics.elide(fm, count_text, layout["count"].width()))

    def _draw_body(self, painter, option, fields, rects):
        palette = option.palette
        fm = QFontMetrics(self._view.font())
        cols = rects["columns"]

        # 组层级左边框（与真实行 groupDepth 一致）
        depth = fields["depth"]
        if depth:
            # 组层级左边框取 palette.Highlight（随浅/深主题变化），与标题同源；
            # 不硬编码固定色，避免深色主题下偏亮。
            accent = QColor(palette.color(QPalette.Highlight))
            bar_w = max(1, depth)
            painter.fillRect(
                QRect(rects["body"].left(), rects["body"].top(), bar_w,
                      rects["body"].height()),
                accent,
            )

        # 编号
        index_font = QFont(self._view.font())
        index_font.setBold(True)
        painter.setFont(index_font)
        index_color = QColor(palette.color(QPalette.WindowText))
        index_color.setAlpha(210)
        painter.setPen(index_color)
        painter.drawText(cols["index"], Qt.AlignCenter,
                         metrics.elide(fm, fields["index"], cols["index"].width()))

        # 标记（沿用视图字体，不引入局部字号；20x20 列居中显示单字）
        if fields["marks"]:
            mark_rect = cols["marks"]
            painter.setFont(self._view.font())
            painter.setPen(QColor(palette.color(QPalette.WindowText)))
            x = mark_rect.left()
            for mark in fields["marks"]:
                cell = QRect(x, mark_rect.top(), metrics.MARK_SIZE,
                             mark_rect.height())
                painter.drawText(cell, Qt.AlignCenter, mark)
                x += metrics.MARK_SIZE + metrics.CONTROLS_SPACING

        # 动作
        painter.setFont(self._view.font())
        painter.setPen(QColor(palette.color(QPalette.WindowText)))
        painter.drawText(cols["type"], Qt.AlignVCenter | Qt.AlignLeft,
                         metrics.elide(fm, fields["type"], cols["type"].width()))

        # 参数
        value_color = QColor(palette.color(QPalette.WindowText))
        value_color.setAlpha(225)
        painter.setPen(value_color)
        painter.drawText(cols["value"], Qt.AlignVCenter | Qt.AlignLeft,
                         metrics.elide(fm, fields["value"], cols["value"].width()))

        # 右侧操作区留白（交互由真实控件承接，不画不可点击的假图标）


def _split_summary(summary):
    """把 "编号．[标记] 动作 | 参数" 拆为 (动作, 参数)。

    标记段 ``[停|组]`` 由 :func:`_summary_marks` 单独提供，必须先从 head 剥离，
    否则动作列会混入 "[组] " 前缀并造成标记重复显示。

    仅按第一个 " | " 拆分，参数内部可能含有 " | "（如多图摘要），保持完整。
    """
    text = str(summary or "").strip()
    head, value = (text.split(" | ", 1) + [""])[:2] if " | " in text else (text, "")
    # head 形如 "15. [组] 点击图片" / "16. [停|组] 等待" / "17. 左键单击"
    # 先去掉编号（首个空白前的 token），再剥离紧随其后的 [..] 标记段。
    rest = head.split(" ", 1)[1] if " " in head else ""
    if rest.startswith("["):
        close = rest.find("]")
        if close >= 0:
            rest = rest[close + 1:].lstrip()
    return rest.strip(), value.strip()


def _summary_marks(summary):
    """从摘要的 [..] 段提取标记列表（停/注/隐/组/断/修）。"""
    text = str(summary or "")
    start = text.find("[")
    end = text.find("]", start + 1)
    if start < 0 or end < 0:
        return []
    inner = text[start + 1:end]
    return [part for part in inner.split("|") if part]
