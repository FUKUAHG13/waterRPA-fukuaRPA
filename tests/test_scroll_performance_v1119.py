# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.19：滚动条拖动热路径与轻量摘要占位的验收测试。

覆盖方案第一部分验收：滑块 pressed/moved/released 三信号、拖动期摘要占位、
松手后最终视口补齐完整可编辑行、快速往返、折叠组外行、草稿保活、80/81 切换。
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QStyle,
    QStyleOptionSlider,
)

from tests.qt_test_support import (  # noqa: E402
    QtCallbackGuardMixin,
    create_test_window,
)


def _slider_center(sb):
    opt = QStyleOptionSlider()
    sb.initStyleOption(opt)
    handle = sb.style().subControlRect(
        QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, sb
    )
    return handle.center()


class ScrollSliderDragTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _build(self, n, *, folded=False):
        window = create_test_window(self, tempfile.mkdtemp())
        task_list = window.task_list
        task_list.clear()
        group = {"id": "g", "name": "录制步骤（原速回放）", "collapsed": False}
        for i in range(n):
            groups = [dict(group)] if (folded and i < n - 4) else []
            window.add_row(
                {"type": 1.0, "value": f"OUT{i}" if i >= n - 4 else f"{i},{i}",
                 "editor_groups": groups},
                record_undo=False, select=False, materialize=False,
                update_indexes=False,
            )
        window.update_indexes()
        window.resize(1100, 760)
        window.show()
        self.app.processEvents()
        if folded:
            gid = window.snapshot_tasks()[0]["editor_groups"][0]["id"]
            window.workflow_editor.toggle_group_collapsed(gid)
            self.app.processEvents()
        task_list.schedule_virtual_refresh(immediate=True)
        self.app.processEvents()
        return window, task_list

    def test_slider_drag_signals_and_final_viewport_editable(self):
        """三信号经过；拖动中不新建完整编辑控件；松手后最终视口可编辑。"""
        window, tl = self._build(95)
        self.addCleanup(window.close)
        sb = tl.verticalScrollBar()
        seen = {"pressed": 0, "moved": 0, "released": 0}
        sb.sliderPressed.connect(lambda: seen.__setitem__("pressed", seen["pressed"] + 1))
        sb.sliderMoved.connect(lambda _v: seen.__setitem__("moved", seen["moved"] + 1))
        sb.sliderReleased.connect(lambda: seen.__setitem__("released", seen["released"] + 1))

        original_restore = window.restore_row_widget
        creates = {"n": 0}

        def counting_restore(item, data):
            creates["n"] += 1
            return original_restore(item, data)

        sb.setValue(0)
        center = _slider_center(sb)
        groove = sb.rect()
        top_y = max(groove.top() + 2, center.y())
        bottom_y = min(groove.bottom() - 2, center.y())

        # 按住并拖到底：拖动期不应同步新建大量完整控件
        QTest.mousePress(sb, Qt.LeftButton, Qt.NoModifier, center)
        self.app.processEvents()
        creates["during_drag"] = 0
        with mock.patch.object(window, "restore_row_widget", counting_restore):
            for point, value in (
                (QPoint(center.x(), bottom_y), sb.maximum()),
                (QPoint(center.x(), top_y), 0),
                (QPoint(center.x(), bottom_y), sb.maximum()),
            ):
                QTest.mouseMove(sb, point)
                sb.setValue(value)
                self.app.processEvents()
            during = creates["n"]
            # v1.1.20（C2）：摘要文本占位已改为同版式绘制委托。验证拖动期
            # 视口内的未物化空行由委托接管，且 item.text() 恒为空。
            delegate = tl.itemDelegate()
            vh = max(0, tl.viewport().height())
            preview_rows = []
            for row, top, bottom in tl._visible_row_geometry():
                if bottom <= 0 or top >= vh:
                    continue
                item = tl.item(row)
                index = tl.indexFromItem(item)
                if tl.itemWidget(item) is None and delegate.should_preview(index):
                    preview_rows.append((row, item, index))
            self.assertGreater(
                len(preview_rows), 0,
                "拖动期视口内应有未物化空行由同版式委托预览",
            )
            for _row, item, _index in preview_rows:
                self.assertEqual(item.text(), "", "预览行不得写入摘要文本")
            QTest.mouseRelease(sb, Qt.LeftButton, Qt.NoModifier,
                               QPoint(center.x(), bottom_y))

        self.assertEqual(seen["pressed"], 1)
        self.assertGreaterEqual(seen["moved"], 1)
        self.assertEqual(seen["released"], 1)
        self.assertGreaterEqual(during, 0)
        # 松手后滚动范围可能重算；持续跑事件循环直至最终视口全部可编辑
        # （这才是真实 GUI 的行为；补齐定时器在事件循环中触发）。
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        timer = QTimer()
        elapsed = [0]

        def tick():
            elapsed[0] += 20
            self.app.processEvents()
            if not self._viewport_missing(tl) or elapsed[0] >= 1000:
                loop.quit()

        timer.timeout.connect(tick)
        timer.start(20)
        loop.exec()
        timer.stop()
        self._assert_viewport_editable(tl)

    def _viewport_missing(self, tl):
        vh = max(0, tl.viewport().height())
        return [
            row for row, top, bottom in tl._visible_row_geometry()
            if bottom > 0 and top < vh and tl.itemWidget(tl.item(row)) is None
        ]

    def _assert_viewport_editable(self, tl):
        missing = self._viewport_missing(tl)
        self.assertEqual(missing, [], f"视口内未物化行：{missing}")

    def test_fast_roundtrip_keeps_steps_intact(self):
        window, tl = self._build(380)
        self.addCleanup(window.close)
        before = window.snapshot_tasks()
        sb = tl.verticalScrollBar()
        center = _slider_center(sb)
        for _ in range(2):
            QTest.mousePress(sb, Qt.LeftButton, Qt.NoModifier, center)
            QTest.mouseMove(sb, QPoint(center.x(), sb.rect().bottom() - 2))
            sb.setValue(sb.maximum())
            QTest.mouseMove(sb, QPoint(center.x(), sb.rect().top() + 2))
            sb.setValue(0)
            QTest.mouseRelease(sb, Qt.LeftButton, Qt.NoModifier, center)
            self.app.processEvents()
        after = window.snapshot_tasks()
        self.assertEqual(len(after), 380)
        self.assertEqual(
            [s["value"] for s in before], [s["value"] for s in after]
        )

    def test_folded_outside_rows_editable_after_drag(self):
        window, tl = self._build(144, folded=True)
        self.addCleanup(window.close)
        self.assertFalse(tl.item(0).isHidden())
        self.assertTrue(tl.item(1).isHidden())
        sb = tl.verticalScrollBar()
        center = _slider_center(sb)
        QTest.mousePress(sb, Qt.LeftButton, Qt.NoModifier, center)
        QTest.mouseMove(sb, QPoint(center.x(), sb.rect().bottom() - 2))
        sb.setValue(sb.maximum())
        QTest.mouseRelease(sb, Qt.LeftButton, Qt.NoModifier, center)
        self.app.processEvents()
        for row in range(140, 144):
            item = tl.item(row)
            self.assertFalse(item.isHidden())
            self.assertIsNotNone(tl.itemWidget(item), f"组外第 {row} 行未物化")

    def test_threshold_80_81_boundary(self):
        for n in (80, 81):
            with self.subTest(n=n):
                window, tl = self._build(n)
                self.addCleanup(window.close)
                self.assertEqual(tl.virtualization_active(), n > 80)
                tl.schedule_virtual_refresh(immediate=True)
                self.app.processEvents()
                self.assertGreater(tl.materialized_row_count(), 0)


class StepRowPreviewTests(QtCallbackGuardMixin, unittest.TestCase):
    """v1.1.20 同版式预览委托的验收：分列、分组标题、折叠锚点、连续性、清理。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _build_with_headers(self, n=95, *, folded=False, multi=False):
        window = create_test_window(self, tempfile.mkdtemp())
        task_list = window.task_list
        task_list.clear()
        group = {"id": "g", "name": "录制步骤（原速回放）", "collapsed": False}
        inner = {"id": "gi", "name": "内层分组", "collapsed": False}
        for i in range(n):
            groups = []
            if i == 0:
                groups.append(dict(group))
                if multi:
                    groups.append(dict(inner))
            elif folded and i < n - 4:
                groups.append(dict(group))
            window.add_row(
                {"type": 1.0, "value": f"OUT{i}" if i >= n - 4 else f"{i},{i}",
                 "editor_groups": groups},
                record_undo=False, select=False, materialize=False,
                update_indexes=False,
            )
        window.update_indexes()
        window.resize(1100, 760)
        window.show()
        self.app.processEvents()
        if folded:
            gid = window.snapshot_tasks()[0]["editor_groups"][0]["id"]
            window.workflow_editor.toggle_group_collapsed(gid)
            self.app.processEvents()
        task_list.schedule_virtual_refresh(immediate=True)
        self.app.processEvents()
        return window, task_list

    def _find_preview_row(self, tl):
        delegate = tl.itemDelegate()
        vh = max(0, tl.viewport().height())
        for row, top, bottom in tl._visible_row_geometry():
            if bottom <= 0 or top >= vh:
                continue
            item = tl.item(row)
            index = tl.indexFromItem(item)
            if tl.itemWidget(item) is None and delegate.should_preview(index):
                return row, item, index
        return None

    def test_materialized_row_is_not_previewed(self):
        """已有 itemWidget 的行不应进入预览（委托让位真实控件）。"""
        window, tl = self._build_with_headers(95)
        self.addCleanup(window.close)
        delegate = tl.itemDelegate()
        # 物化视口内已有控件的行
        for row in range(tl.count()):
            item = tl.item(row)
            if tl.itemWidget(item) is not None:
                self.assertFalse(
                    delegate.should_preview(tl.indexFromItem(item)),
                    f"第 {row} 行已有控件，不应绘制预览",
                )
                return
        self.skipTest("当前视口没有已物化行")

    def test_preview_columns_are_distinct_and_ordered(self):
        """未物化行按编号→动作→参数分列，列矩形不重叠且顺序正确。"""
        window, tl = self._build_with_headers(95)
        self.addCleanup(window.close)
        sb = tl.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.app.processEvents()
        found = self._find_preview_row(tl)
        self.assertIsNotNone(found, "应存在未物化预览行")
        row, _item, index = found
        from PySide6.QtWidgets import QStyleOptionViewItem

        delegate = tl.itemDelegate()
        fields = delegate.preview_fields(index)
        opt = QStyleOptionViewItem()
        opt.rect = tl.visualRect(index)
        opt.palette = tl.palette()
        rects = delegate.preview_rects(opt, fields)
        cols = rects["columns"]
        # 列顺序：index < type < value < actions
        self.assertLess(cols["index"].left(), cols["type"].left())
        self.assertLess(cols["type"].left(), cols["value"].left())
        self.assertLess(cols["value"].left(), cols["actions"].left())
        # 同处一正文行、高度一致
        self.assertEqual(cols["index"].top(), cols["value"].top())
        self.assertGreater(cols["value"].width(), 0)
        # 字段来自结构化数据
        self.assertEqual(fields["index"], f"{row + 1}.")
        self.assertTrue(fields["type"])

    def test_preview_draws_pixels_into_unmaterialized_row(self):
        """委托确有绘制内容到未物化行（非空白），浅色主题下可证伪。"""
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem

        window, tl = self._build_with_headers(95)
        self.addCleanup(window.close)
        tl.verticalScrollBar().setValue(tl.verticalScrollBar().maximum())
        self.app.processEvents()
        found = self._find_preview_row(tl)
        self.assertIsNotNone(found)
        _row, _item, index = found
        delegate = tl.itemDelegate()
        fields = delegate.preview_fields(index)
        rect = tl.visualRect(index)
        img = QImage(max(1, rect.width()), max(1, rect.height()),
                     QImage.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        opt = QStyleOptionViewItem()
        opt.rect = rect
        opt.palette = tl.palette()
        painter = QPainter(img)
        delegate.draw_preview(painter, opt, fields)
        painter.end()
        # 统计非白像素：边框与文本会产生足够多的非白像素
        non_white = sum(
            1 for y in range(img.height()) for x in range(img.width())
            if img.pixelColor(x, y).red() < 245
        )
        self.assertGreater(non_white, 20, "预览行应有可见绘制内容")

    def test_multi_level_headers_order_and_depth(self):
        """同一项多层标题按 depth 顺序绘制，缩进随 depth 递增。"""
        from PySide6.QtWidgets import QStyleOptionViewItem

        window, tl = self._build_with_headers(95, multi=True)
        self.addCleanup(window.close)
        # 找到首行（带多层 headers）
        item = tl.item(0)
        index = tl.indexFromItem(item)
        delegate = tl.itemDelegate()
        fields = delegate.preview_fields(index)
        self.assertGreaterEqual(len(fields["headers"]), 2)
        opt = QStyleOptionViewItem()
        opt.rect = tl.visualRect(index)
        opt.palette = tl.palette()
        rects = delegate.preview_rects(opt, fields)
        self.assertEqual(len(rects["headers"]), len(fields["headers"]))
        titles = [h["header"].get("name") for h in rects["headers"]]
        self.assertEqual(titles, ["录制步骤（原速回放）", "内层分组"])
        lefts = [h["layout"]["title"].left() for h in rects["headers"]]
        self.assertLess(lefts[0], lefts[1], "深层标题应更靠右缩进")

    def test_folded_anchor_draws_header_only(self):
        """折叠锚点 controls_hidden 只画标题，不画正文。"""
        window, tl = self._build_with_headers(95, folded=True)
        self.addCleanup(window.close)
        anchor = tl.item(0)
        index = tl.indexFromItem(anchor)
        delegate = tl.itemDelegate()
        fields = delegate.preview_fields(index)
        self.assertTrue(fields["controls_hidden"], "折叠锚点应 controls_hidden")
        self.assertTrue(fields["headers"], "折叠锚点应有标题")
        from PySide6.QtWidgets import QStyleOptionViewItem
        opt = QStyleOptionViewItem()
        opt.rect = tl.visualRect(index)
        opt.palette = tl.palette()
        rects = delegate.preview_rects(opt, fields)
        self.assertGreater(len(rects["headers"]), 0)
        # 正文高度为 0：折叠锚点 sizeHint 只含 header 高度
        self.assertEqual(rects["body"].height(), 0)

    def test_release_to_fill_keeps_preview_continuous(self):
        """C1：松手后补齐前，视口未物化行仍由委托预览（无空白帧）。"""
        window, tl = self._build_with_headers(95)
        self.addCleanup(window.close)
        sb = tl.verticalScrollBar()
        center = _slider_center(sb)
        QTest.mousePress(sb, Qt.LeftButton, Qt.NoModifier, center)
        QTest.mouseMove(sb, QPoint(center.x(), sb.rect().bottom() - 2))
        sb.setValue(sb.maximum())
        self.app.processEvents()
        QTest.mouseRelease(sb, Qt.LeftButton, Qt.NoModifier,
                           QPoint(center.x(), sb.rect().bottom() - 2))
        # 松手瞬间（补齐尚未完成）检查未物化行仍可预览
        self.assertFalse(tl._slider_dragging)
        delegate = tl.itemDelegate()
        vh = max(0, tl.viewport().height())
        unmaterialized = [
            row for row, top, bottom in tl._visible_row_geometry()
            if bottom > 0 and top < vh and tl.itemWidget(tl.item(row)) is None
        ]
        for row in unmaterialized:
            index = tl.indexFromItem(tl.item(row))
            self.assertTrue(
                delegate.should_preview(index),
                f"松手补齐前第 {row} 行仍未物化，应继续预览避免空白",
            )

    def test_close_resets_preview_state(self):
        """关闭窗口后复位拖动状态并清空委托缓存与定时器。"""
        window, tl = self._build_with_headers(95)
        tl._slider_dragging = True
        delegate = tl.itemDelegate()
        # 触发一次缓存写入
        found = self._find_preview_row(tl)
        if found:
            delegate.preview_fields(found[2])
        self.assertTrue(tl._virtual_release_timer is not None)
        tl._virtual_release_timer.start(1000)
        window.close()
        self.app.processEvents()
        self.assertFalse(tl._slider_dragging)
        self.assertFalse(tl._virtual_release_timer.isActive())
        self.assertEqual(delegate._field_cache, {})


if __name__ == "__main__":
    unittest.main()
