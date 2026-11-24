# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.17 修复 1：折叠组后组外步骤行控件消失的红绿对照。

同一断言（组外四行各有真实控件）在基线上失败、修复后通过。
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.qt_test_support import (  # noqa: E402
    QtCallbackGuardMixin,
    create_test_window,
)

TOTAL_ROWS = 144
OUTSIDE_VALUES = ["AAA,111", "BBB,222", "CCC,333", "DDD,444"]


class CollapsedOutsideVisibilityTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _build_window(self, base_dir):
        window = create_test_window(self, base_dir)
        task_list = window.task_list
        task_list.clear()
        # 144 行：0..139 进折叠组，140..143 为组外可区分步骤。
        group = {"id": "rec_group", "name": "录制步骤（原速回放）", "collapsed": False}
        for index in range(TOTAL_ROWS):
            editor_groups = [dict(group)] if index < TOTAL_ROWS - 4 else []
            window.add_row(
                {"type": 1.0, "value": OUTSIDE_VALUES[index - (TOTAL_ROWS - 4)]
                 if index >= TOTAL_ROWS - 4 else f"{index},{index}",
                 "editor_groups": editor_groups},
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        window.update_indexes()
        window.show()
        self.app.processEvents()
        # 折叠组：组内行隐藏，组标题锚点行保持可见。
        snapshot = window.snapshot_tasks()
        group_id = snapshot[0]["editor_groups"][0]["id"]
        window.workflow_editor.toggle_group_collapsed(group_id)
        self.app.processEvents()
        # 滚动到列表底部：视口底部落在组外行之后的区域。
        last = task_list.item(TOTAL_ROWS - 1)
        task_list.scrollToItem(last)
        self.app.processEvents()
        task_list.schedule_virtual_refresh(immediate=True)
        self.app.processEvents()
        return window, task_list

    def _outside_rows_visible(self, task_list):
        """组外四行各有真实控件且未隐藏。"""
        result = []
        for index in range(TOTAL_ROWS - 4, TOTAL_ROWS):
            item = task_list.item(index)
            widget = task_list.itemWidget(item)
            result.append(
                item is not None
                and not item.isHidden()
                and widget is not None
            )
        return result

    def test_collapsed_group_keeps_outside_rows_materialized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window, task_list = self._build_window(temp_dir)
            # 折叠标题锚点行可见，组内行隐藏。
            self.assertFalse(task_list.item(0).isHidden())
            self.assertTrue(task_list.item(1).isHidden())
            # 组外四步快照 ID/顺序/值正确。
            snapshot = window.snapshot_tasks()
            self.assertEqual(len(snapshot), TOTAL_ROWS)
            self.assertEqual(
                [snapshot[i]["value"] for i in range(TOTAL_ROWS - 4, TOTAL_ROWS)],
                OUTSIDE_VALUES,
            )
            self.assertEqual(
                [snapshot[i].get("editor_groups", []) for i in range(TOTAL_ROWS - 4, TOTAL_ROWS)],
                [[], [], [], []],
            )
            # 核心断言：四行各有真实控件。
            self.assertEqual(
                self._outside_rows_visible(task_list),
                [True, True, True, True],
            )

    def test_toggle_collapse_repeatedly_keeps_outside_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window, task_list = self._build_window(temp_dir)
            snapshot = window.snapshot_tasks()
            group_id = snapshot[0]["editor_groups"][0]["id"]
            for _ in range(3):
                window.workflow_editor.toggle_group_collapsed(group_id)
                self.app.processEvents()
                # 每次切换后重新把组外行带回视口，断言其矩形在视口内时
                # 必有真实控件（展开态视口停在顶部时组外行本就不可见）。
                task_list.scrollToItem(task_list.item(TOTAL_ROWS - 1))
                self.app.processEvents()
                task_list.schedule_virtual_refresh(immediate=True)
                self.app.processEvents()
                self.assertEqual(
                    self._outside_rows_visible(task_list),
                    [True, True, True, True],
                )

    def test_buffer_extends_beyond_viewport_visible_rows(self):
        # 缓冲按可见项向上下扩展 VIRTUAL_BUFFER_ROWS：物化集合应大于视口
        # 相交项集合。用 _target_materialized_rows 直接核对，避免滚动定时器
        # 在离屏下的时序不确定性（见下一条走真实 valueChanged 的用例）。
        with tempfile.TemporaryDirectory() as temp_dir:
            window, task_list = self._build_window(temp_dir)
            target = task_list._target_materialized_rows()
            viewport_height = task_list.viewport().height()
            intersecting = {
                row
                for row, top, bottom in task_list._visible_row_geometry()
                if bottom > 0 and top < viewport_height
            }
            # 折叠态可见项本就少（标题+组外四行），缓冲会把它们全部纳入；
            # 至少不应少于视口相交集合。
            self.assertTrue(intersecting.issubset(target))
            self.assertGreaterEqual(len(target), len(intersecting))

            # 严格锁定缓冲生效：换成长列表未折叠场景，视口只覆盖部分行，
            # target 必须比视口相交集合多出下侧缓冲可见项（否则旧实现
            # lo=0/hi=n 会通过上面的弱断言，却丢失缓冲）。
            task_list.clear()
            for index in range(150):
                window.add_row(
                    {"type": 1.0, "value": f"{index},{index}"},
                    record_undo=False,
                    select=False,
                    materialize=False,
                    update_indexes=False,
                )
            window.update_indexes()
            self.app.processEvents()
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()
            geom2 = task_list._visible_row_geometry()
            vh2 = task_list.viewport().height()
            inter2 = {r for r, top, bottom in geom2 if bottom > 0 and top < vh2}
            target2 = task_list._target_materialized_rows()
            self.assertTrue(inter2.issubset(target2))
            self.assertGreater(len(target2), len(inter2))

    def test_scrollbar_value_changed_refreshes_without_immediate(self):
        # 阻断项回归：不调用 immediate，改由滚动条 valueChanged 触发 50ms
        # 延迟刷新，确认折叠后组外行仍能被物化（真实滚动路径）。
        from PySide6.QtCore import QEventLoop, QTimer

        with tempfile.TemporaryDirectory() as temp_dir:
            window, task_list = self._build_window(temp_dir)
            # 复位到列表顶部，再滚动到底：触发 valueChanged → 延迟刷新。
            task_list.verticalScrollBar().setValue(0)
            self.app.processEvents()
            task_list.verticalScrollBar().setValue(
                task_list.verticalScrollBar().maximum()
            )
            # 等待延迟刷新定时器（50ms）真正执行。
            loop = QEventLoop()
            QTimer.singleShot(180, loop.quit)
            loop.exec()
            self.app.processEvents()
            self.assertEqual(
                self._outside_rows_visible(task_list),
                [True, True, True, True],
            )

    def test_deselect_last_step_still_shows_outside_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window, task_list = self._build_window(temp_dir)
            # 取消选中第 148 步：去掉 current 行的额外保活。
            task_list.clearSelection()
            task_list.setCurrentRow(-1)
            self.app.processEvents()
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()
            self.assertEqual(
                self._outside_rows_visible(task_list),
                [True, True, True, True],
            )


if __name__ == "__main__":
    unittest.main()
