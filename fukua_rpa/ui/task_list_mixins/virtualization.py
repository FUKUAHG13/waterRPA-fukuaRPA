# pyright: reportAttributeAccessIssue=false
"""DraggableListWidget 的虚拟化 Mixin（God Class 拆分）。

从 ``DraggableListWidget`` 提取的方法组：虚拟行高提示、按窗口可见区间
计算物化目标行、以及在滚动时物化/去物化行控件的刷新逻辑。实例状态
（``_virtual_*``、``_materialized_*``、``_drag_active`` 守卫等）仍由主类
``__init__`` 创建，本 Mixin 不持有任何状态，跨 Mixin 调用经 MRO 解析。

v1.1.19：几何定位改为「常数次 indexAt 锚点 + 局部 visualRect」，只在定位
出的物理行区间内查询，避免每次刷新遍历全部物理行。滚动条拖动期以轻量摘要
占位替代同步新建完整编辑控件，松开后按最终视口补齐。
"""

import time

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QApplication

from ..performance_hooks import ui_metrics


class VirtualizationMixin:
    # indexAt 锚点查找的最大步数上界（防止异常布局下无界扫描）。
    _ANCHOR_SEARCH_LIMIT = 512

    def show_drag_placeholders(self):
        """Expose summaries for non-materialized rows during a manual drag."""

        # INVARIANT（v1.1.19）：手动拖拽占位与滚动条占位互斥。进入手动拖拽前
        # 先清掉滚动条占位，避免两套列表对同一 item 交叉 setText/clear。
        self._clear_scroll_placeholders()
        for row in range(self.count()):
            item = self.item(row)
            if (
                item is None
                or item.isHidden()
                or self.itemWidget(item) is not None
                or item.text()
            ):
                continue
            summary = str(item.data(Qt.UserRole + 1) or "").strip()
            if not summary:
                continue
            item.setText(summary)
            # 记录 (row, item, original_text) 三元组，clear 时按行核对。
            self._drag_placeholder_items.append((row, item, ""))

    def clear_drag_placeholders(self):
        """Clear only texts installed by :meth:`show_drag_placeholders`."""

        for row, item, original in self._drag_placeholder_items:
            if item is not None and self.row(item) == row:
                item.setText(original)
        self._drag_placeholder_items.clear()

    def _install_scroll_placeholders(self, target):
        """v1.1.20：滚动期占位改为委托绘制，本方法不再写入 ``item.text()``。

        预览委托（``StepRowPreviewDelegate``）对未物化空行无条件绘制同版式预览，
        纯文本摘要占位已无必要。保留方法签名与拖动期调用点，仅维护一个供测试
        观察的视口行集合；手动拖拽摘要路径 ``show_drag_placeholders`` 不受影响。

        INVARIANT：不改写任何 ``item.text()`` / data / sizeHint；不新建控件。
        """
        if self._drag_active:
            return
        drag_rows = {dr for dr, _di, _dt in self._drag_placeholder_items}
        rows = []
        for row in sorted(target):
            if not (0 <= row < self.count()):
                continue
            item = self.item(row)
            if item is None or item.isHidden() or self.itemWidget(item) is not None:
                continue
            if item.text() or row in drag_rows:
                continue
            if not isinstance(item.data(Qt.UserRole), dict):
                continue
            rows.append(row)
        # 记录 (row, item, "") 三元组以维持既有数据结构与清空核对语义；
        # 第三项恒为空字符串，因为不再写入文本。
        self._scroll_placeholder_items = [(row, self.item(row), "") for row in rows]

    def _clear_scroll_placeholders(self):
        """v1.1.20：清空预览记录，不触碰任何 item 的文本。"""
        self._scroll_placeholder_items.clear()

    def _on_slider_pressed(self):
        """滑块按下：进入拖动态并冻结目标集合（保证快速往返可复用）。"""
        if self._drag_active:
            return
        self._slider_dragging = True
        # 冻结语义：拖动期不推进 _last_target_materialized（它保持按下时的
        # 旧视口快照），target 仍按当前视口计算用于摘要与回收判断。

    def _on_slider_moved(self, _value=None):
        """滑块移动：安装轻量摘要占位，不新建完整 TaskRow。"""
        if not self._slider_dragging or self._drag_active:
            return
        if self.virtualization_active():
            self._install_scroll_placeholders(self._target_materialized_rows())

    def _on_slider_released(self):
        """滑块释放：按最终视口补齐完整可编辑行，复位拖动态。"""
        if not self._slider_dragging:
            return
        # 清拖动期占位，避免摘要文本遮挡补齐后的行控件。
        self._clear_scroll_placeholders()
        self._slider_dragging = False
        # INVARIANT：补齐必须用「滚动条稳定后」的最终视口。sliderReleased 回调
        # 触发瞬间 value 可能处于释放点中间态甚至越界，且滚动范围仍在重算（实测
        # max 88→87 延迟到后续事件循环），此刻算 target 会漏行。清空拖动期冻结
        # 的旧集合，使后续 refresh 的 scan=target|空集 恰为最终视口，不物化拖动
        # 路径上的额外行。
        self._last_target_materialized = set()
        # DESIGN：不在此刻主动刷新（value 未稳定）。范围重算时 Qt 会再发
        # valueChanged/rangeChanged，其 50ms 合并刷新按稳定后的最终视口补齐；
        # 若 value 未再变化，则安排一次兜底刷新，保证松手后一定按需补齐。
        if not self._virtual_refresh_timer.isActive():
            self._virtual_release_timer.start(self.VIRTUAL_REFRESH_INTERVAL_MS)

    def _deferred_complete_release(self):
        """兜底补齐：value/max 稳定后按最终视口补齐完整可编辑行。"""
        if self._slider_dragging:
            return
        sb = self.verticalScrollBar()
        value = sb.value()
        clamped = max(sb.minimum(), min(sb.maximum(), value))
        if clamped != value:
            sb.setValue(clamped)
        self.refresh_virtual_widgets()

    def virtual_row_size_hint(self):
        return QSize(
            0,
            max(
                int(self._virtual_min_row_height),
                int(self._virtual_row_height),
            ),
        )

    def group_row_size_hint(self, presentation=None):
        presentation = presentation if isinstance(presentation, dict) else {}
        header_count = len(presentation.get("headers", []))
        controls_height = (
            0
            if presentation.get("controls_hidden")
            else self._virtual_row_height
        )
        header_height = int(self._virtual_group_header_height)
        return QSize(
            0,
            max(
                header_height,
                int(controls_height + header_count * header_height),
            ),
        )

    def update_virtual_row_height(self, scale):
        effective_scale = max(0.375, min(5.4, float(scale)))
        self._virtual_min_row_height = max(
            1, int(round(42 * effective_scale))
        )
        self._virtual_group_header_height = max(
            1, int(round(32 * effective_scale))
        )
        self._virtual_row_height = max(
            self._virtual_min_row_height,
            int(round(self.BASE_ROW_HEIGHT * effective_scale)),
        )
        # v1.1.20：行高/缩放变化后预览委托的字体与几何失效，清空其只读缓存。
        delegate = getattr(self, "_preview_delegate", None)
        if delegate is not None and hasattr(delegate, "invalidate"):
            delegate.invalidate()
        size_hint = self.virtual_row_size_hint()
        for row in range(self.count()):
            item = self.item(row)
            if self.itemWidget(item) is None:
                presentation = item.data(Qt.UserRole + 2)
                item.setSizeHint(
                    self.group_row_size_hint(presentation)
                    if isinstance(presentation, dict)
                    else size_hint
                )
        self.schedule_virtual_refresh()

    def virtualization_active(self):
        return self.count() > self.VIRTUALIZATION_THRESHOLD

    def materialized_row_count(self):
        # O(1) when the incremental counter is valid (item count unchanged
        # since last refresh).  Falls back to an O(n) full-list scan when
        # the cache is invalidated — e.g. after items are added/removed or
        # external code (ensure_task_item_widget) materializes widgets
        # without notifying this class.
        total = self.count()
        if self._materialized_count is not None and self._materialized_count_items == total:
            return self._materialized_count
        count = sum(
            1
            for row in range(total)
            if self.itemWidget(self.item(row)) is not None
        )
        self._materialized_count = count
        self._materialized_count_items = total
        return count

    def schedule_virtual_refresh(self, immediate=False):
        if self._virtual_refreshing or self._drag_active:
            return
        if immediate:
            self._virtual_refresh_timer.stop()
            self.refresh_virtual_widgets()
            return
        if self._virtual_refresh_timer.isActive():
            return
        self._virtual_refresh_timer.start(self.VIRTUAL_REFRESH_INTERVAL_MS)

    # ------------------------------------------------------------------
    # v1.1.19：常数次视口锚点定位 + 局部几何
    # ------------------------------------------------------------------
    def _index_at_viewport(self, y):
        """Return the non-hidden physical row at viewport y, or None.

        ``indexAt`` 只命中参与布局（未隐藏）的项；折叠标题锚点保持未隐藏，
        因此可被命中。视口落在空白区 / 全折叠区时返回 None，由调用方回退。
        """
        height = max(0, self.viewport().height() - 1)
        y = max(0, min(height, int(y)))
        index = self.indexAt(QPoint(1, y))
        if not index.isValid():
            return None
        row = index.row()
        item = self.item(row)
        if item is None or item.isHidden():
            return None
        return row

    def _nearest_visible_row(self, start, step):
        """从 start 沿 step 方向找最近未隐藏物理行；有界，找不到返回 None。"""
        total = self.count()
        row = start + step
        scanned = 0
        while 0 <= row < total and scanned < self._ANCHOR_SEARCH_LIMIT:
            item = self.item(row)
            if item is not None and not item.isHidden():
                return row
            row += step
            scanned += 1
        return None

    def _next_visible_row(self, start):
        """Return the next non-hidden physical row strictly after ``start``.

        有界线性查找；找不到返回 None。用于按「可见顺序」而非物理行号推进。
        """
        return self._nearest_visible_row(start, 1)

    def _viewport_row_anchor(self):
        """Return the inclusive ``(top_row, bottom_row)`` physical-row interval.

        DESIGN（v1.1.19）：以视口顶部的「可见顺序锚点」为起点，向后走有限个
        未隐藏行覆盖视口 + 缓冲，再取为物理行区间。这样对长列表（可见项密集）
        访问量与总行数无关；对折叠态（可见项稀疏）自然覆盖到跨隐藏区间的组外行，
        不依赖 indexAt 在空白视口的歧义返回值。

        三段式回退（顶部）：① ``indexAt(1,1)`` 命中有效行即用；② 无效时沿可见
        顺序向上找最近未隐藏项（从物理 0 起）；③ 找不到回退 0。顶部三部全部
        无效（整表无可见项）时返回 ``(0, count()-1)`` 保守上界。

        步数上界 ``span = ceil(viewport_height / min_row_height) + 2*buffer + 2``
        为常数级（与总行数无关）；折叠态可见项少于 span 时走到表尾即停。任何
        放大只影响性能上界——``_visible_row_geometry`` 仍逐行 ``isHidden()`` 过滤、
        target 只取相交段+缓冲，不改变正确性。
        """
        total = self.count()
        if total <= 0:
            return (0, -1)
        height = max(1, int(self.viewport().height()))
        min_h = max(1, int(getattr(self, "_virtual_min_row_height", 42)))
        span = height // min_h + 2 * self.VIRTUAL_BUFFER_ROWS + 2

        top_hit = self._index_at_viewport(1)
        if top_hit is None:
            # 回退：沿可见顺序从表头找第一个未隐藏项；整表全隐藏回退 0。
            first = self._nearest_visible_row(-1, 1)
            top_row = first if first is not None else 0
        else:
            top_row = top_hit

        # 沿可见顺序向后推进至多 span 步，取最后一个未隐藏物理行作为上界。
        bottom_row = top_row
        row = top_row
        for _ in range(span):
            nxt = self._next_visible_row(row)
            if nxt is None:
                break
            bottom_row = nxt
            row = nxt

        if bottom_row < top_row:
            top_row, bottom_row = bottom_row, top_row
        return (top_row, bottom_row)

    def _visible_row_geometry(self, anchor=None):
        """List of ``(physical_row, top, bottom)`` for non-hidden items.

        DESIGN：折叠组的隐藏行仍占物理行号，但不能按物理行号 + 估算行数推导
        视口末端。``anchor`` 给出由 :meth:`_viewport_row_anchor` 计算的紧凑物理
        区间，只在区间内逐行查 ``visualRect``；折叠标题锚点保持 ``isHidden()==False``
        因而自然计入。区间外的行不查询，几何查询次数从 O(总行数) 降为 O(视口行数)。

        传入 ``anchor=None`` 时回退为全量遍历（供既有测试/降级路径使用）。
        """
        geometry = []
        total = self.count()
        if anchor is None:
            rows = range(total)
        else:
            top, bottom = anchor
            rows = range(max(0, int(top)), min(total, int(bottom) + 1))
        for row in rows:
            item = self.item(row)
            self._virtual_item_accesses += 1
            if item is None or item.isHidden():
                continue
            rect = self.visualRect(self.indexFromItem(item))
            self._virtual_geometry_queries += 1
            if rect.isValid():
                geometry.append((row, rect.top(), rect.bottom()))
        return geometry

    def _target_materialized_rows(self):
        count = self.count()
        if count <= self.VIRTUALIZATION_THRESHOLD:
            return set(range(count))
        anchor = self._viewport_row_anchor()
        geometry = self._visible_row_geometry(anchor)
        viewport_height = max(0, int(self.viewport().height()))

        def intersecting_indexes(geom):
            return [
                idx
                for idx, (_row, top, bottom) in enumerate(geom)
                if bottom > 0 and top < viewport_height
            ]

        intersecting = intersecting_indexes(geometry)
        if not intersecting and geometry:
            # DESIGN（v1.1.19）：锚点区间内无相交不等于"视口全空"——编辑器关闭、
            # 行高重算等瞬态会让锚点区间的几何短暂失效。回退到全量几何重新定位，
            # 避免误取区间首尾（曾导致滚到顶部后关闭时序编辑器、行 0 被错误保活）。
            # 这是低频降级路径，不影响常规滚动的 O(视口) 上界。
            geometry = self._visible_row_geometry()
            intersecting = intersecting_indexes(geometry)
        if geometry:
            if intersecting:
                buffer = self.VIRTUAL_BUFFER_ROWS
                lo = max(0, intersecting[0] - buffer)
                hi = min(len(geometry), intersecting[-1] + buffer + 1)
                target = {geometry[idx][0] for idx in range(lo, hi)}
            else:
                # 全量几何也无相交（全部折叠后滚到空白区）：只保活首/末可见项，
                # 不盲目物化整段物理行。
                target = {geometry[0][0], geometry[-1][0]}
        else:
            target = set()
        current = self.currentRow()
        if 0 <= current < count:
            target.add(current)
        active_editor = self.active_timeline_editor_item()
        if active_editor is not None:
            target.add(self.row(active_editor))
        return target

    def refresh_virtual_widgets(self):
        if self._virtual_refreshing or self._drag_active:
            return
        owner = self.window()
        restore = getattr(owner, "restore_row_widget", None)
        if not callable(restore):
            return
        self._virtual_refreshing = True
        started_ns = time.perf_counter_ns()
        dragging = bool(self._slider_dragging)
        try:
            self.setUpdatesEnabled(False)
            target = self._target_materialized_rows()
            if dragging:
                # 拖动期：摘要占位由 _on_slider_moved 即时安装；这里按当前视口
                # 重建一次，保证与回收判断使用同一 target，且不新建完整控件。
                self._install_scroll_placeholders(target)
            focus = QApplication.focusWidget()
            # INVARIANT（v1.1.19）：普通滚动 scan=target|_last_target_materialized
            # 以便及时回写旧视口行。拖动期只用当前 target：快速往返时不逐帧重建
            # 路径上的行；_last_target_materialized 在拖动期保持按下时的快照不动，
            # 旧视口行的残留 widget 在松手补齐时由最终视口统一回收。
            scan = target if dragging else (target | self._last_target_materialized)
            materialized_delta = 0
            for row in sorted(scan):
                if not (0 <= row < self.count()):
                    continue
                item = self.item(row)
                widget = self.itemWidget(item)
                if widget is None:
                    if row in target and not dragging:
                        # 拖动期不新建完整 TaskRow；摘要占位已覆盖空行。
                        data = item.data(Qt.UserRole)
                        if isinstance(data, dict):
                            restore(item, data)
                            materialized_delta += 1
                    continue
                dialog = getattr(widget, "config_dialog", None)
                try:
                    keeps_dialog = bool(
                        dialog is not None and dialog.isVisible()
                    )
                except RuntimeError:
                    keeps_dialog = False
                picker = getattr(widget, "coordinate_picker", None)
                try:
                    keeps_picker = bool(picker is not None and picker.isVisible())
                except RuntimeError:
                    keeps_picker = False
                keeps_focus = bool(
                    focus is not None
                    and (focus is widget or widget.isAncestorOf(focus))
                )
                # DESIGN：保活只认当前 target 与真实编辑器/焦点状态。
                # keep_set（拖动期冻结的 _last_target_materialized）仅用于扩大
                # scan 候选范围以便及时回写，绝不是保活理由——否则滚走的旧行
                # 会一直不被回收（v1.1.19 曾误加导致回归，已修正）。
                if row in target or keeps_dialog or keeps_picker or keeps_focus:
                    continue
                # INVARIANT：回收前必须完成草稿回写，草稿不丢。摘要占位只安装在
                # itemWidget is None 的空行上，不与已物化控件的 item.text() 竞争。
                try:
                    data = widget.get_data()
                    summary = widget.drag_summary()
                except RuntimeError:
                    # C++ object already deleted
                    continue
                item.setData(Qt.UserRole, data)
                item.setData(Qt.UserRole + 1, summary)
                item.setSizeHint(
                    self.group_row_size_hint(item.data(Qt.UserRole + 2))
                )
                self.removeItemWidget(item)
                materialized_delta -= 1
                widget.hide()
                widget.deleteLater()
            if not dragging:
                self._last_target_materialized = target
            # Update the incremental materialized-row counter.
            total = self.count()
            if self._materialized_count is not None and self._materialized_count_items == total:
                self._materialized_count += materialized_delta
            else:
                self._materialized_count = None
                self._materialized_count_items = -1
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.increment("ui.task_list.virtual_refreshes")
                metrics.set_gauge("ui.task_list.total_rows", total)
                metrics.set_gauge(
                    "ui.task_list.materialized_rows",
                    self.materialized_row_count(),
                )
        finally:
            self.setUpdatesEnabled(True)
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.observe_since("ui.task_list.virtual_refresh", started_ns)
            self._virtual_refreshing = False
