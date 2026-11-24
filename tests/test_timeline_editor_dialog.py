# ruff: noqa: E402
"""录制编辑面板的离屏测试：主界面入口、列表、预览、编辑与撤销。

离屏测试覆盖布局与状态契约；真实字体、多屏 DPI 与 Hook 属发布前实机门禁。
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton  # noqa: E402

from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE  # noqa: E402
from fukua_rpa.ui.controllers.workflow_timeline_edit import (  # noqa: E402
    is_raw_timeline_task,
)
from fukua_rpa.ui.lifecycle import widget_is_alive  # noqa: E402
from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402
from fukua_rpa.ui.timeline_editor_dialog import (  # noqa: E402
    TimelineEditorPanel,
    wait_gap_choices,
)


def _raw_task():
    events = []
    for i in range(40):
        events.append(
            {"t": round(i * 0.008, 3), "kind": "move", "x": 100 + i, "y": 200}
        )
    events.insert(
        0, {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 100, "y": 200}
    )
    events.append(
        {"t": round(40 * 0.008, 3), "kind": "mouse", "button": "left", "action": "up", "x": 139, "y": 200}
    )
    return {
        "type": TASK_TYPE_INPUT_TIMELINE,
        "value": "原样回放：42 个事件 / 0.32 秒",
        "input_events": events,
        "recorded_duration": 0.32,
        "playback_mode": "raw",
        "tool_only": True,
    }


class TimelineEditorPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dlg = TimelineEditorPanel(_raw_task())
        self.addCleanup(self._dispose)

    def _dispose(self):
        dlg = getattr(self, "dlg", None)
        if dlg is not None:
            dlg.close()
            dlg.deleteLater()
        self.app.processEvents()

    def _select_row(self, dlg, row):
        dlg.event_list.setCurrentItem(dlg.event_list.topLevelItem(row))
        self.app.processEvents()

    def test_same_window_has_timeline_list_and_preview(self):
        self.assertIsNotNone(self.dlg.findChild(__import__(
            "PySide6.QtWidgets", fromlist=["QWidget"]
        ).QWidget, "timelineTrack"))
        self.assertIsNotNone(self.dlg.findChild(type(self.dlg.event_list), "timelineEventList"))
        self.assertIsNotNone(self.dlg.findChild(QLabel, "timelineDetail"))
        self.assertGreater(self.dlg.event_list.topLevelItemCount(), 0)

    def test_visible_range_zooms_and_lazy_loads_rows(self):
        # 行式改版：连续移动默认折叠为一行（阶段一要求），可见行远小于事件数。
        folded = self.dlg.event_list.topLevelItemCount()
        self.assertLess(folded, 10, "连续移动应折叠显示，不应逐点成行")
        # 展开折叠行后，可见范围仍按时间窗口裁剪：放大后可见行减少。
        self.dlg._expanded_ids = {
            row.id for row in self.dlg._display_rows if row.expandable
        }
        self.dlg._reload()
        full = self.dlg.event_list.topLevelItemCount()
        self.assertGreater(full, folded, "展开后应显示更多行")
        idx = self.dlg.zoom.findData(8)
        self.dlg.zoom.setCurrentIndex(idx)
        self.app.processEvents()
        zoomed = self.dlg.event_list.topLevelItemCount()
        self.assertLess(zoomed, full, "放大后可见行应减少")
        self.assertGreater(zoomed, 0)

    def test_expansion_does_not_change_events(self):
        before = self.dlg.edited_events()
        self.dlg._expanded_ids = {
            row.id for row in self.dlg._display_rows if row.expandable
        }
        self.dlg._reload()
        self.assertEqual(self.dlg.edited_events(), before, "折叠/展开只改视图，不改事件")

    def test_row_level_buttons_follow_model_capability(self):
        # 每行操作列胶囊由 capability 驱动：完整单击含改位置/删除，无折叠入口。
        dlg = TimelineEditorPanel(
            {
                "type": TASK_TYPE_INPUT_TIMELINE,
                "input_events": [
                    {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 10, "y": 20},
                    {"t": 0.1, "kind": "mouse", "button": "left", "action": "up", "x": 10, "y": 20},
                ],
                "playback_mode": "raw",
            }
        )
        self.addCleanup(self._dispose_extra, dlg)
        for row in range(dlg.event_list.topLevelItemCount()):
            item = dlg.event_list.topLevelItem(row)
            chips = tuple(item.data(3, Qt.UserRole + 1) or ())
            actions = tuple(action for action, _label in chips)
            self.assertEqual(actions[0], "detail")
            self.assertIn("edit_pos", actions)
            self.assertIn("delete", actions)
            self.assertNotIn("expand", actions)

    def test_selection_shows_detail_and_canvas_cursor(self):
        self._select_row(self.dlg, 0)
        self.app.processEvents()
        self.assertIn("→", self.dlg.detail.text())
        self.dlg.cursor_spin.setValue(0.1)
        self.app.processEvents()
        self.assertAlmostEqual(self.dlg.preview.cursor_t, 0.1)

    def test_delete_then_undo_restores_copy(self):
        original = self.dlg.edited_events()
        self._select_row(self.dlg, 0)
        # 选中一个按键类动作更安全：这里直接测撤销栈模型层
        self.dlg._undo_stack.append([dict(e) for e in original])
        changed = [dict(e) for e in original[:-1]]
        self.dlg._events = changed
        self.dlg._reload()
        self.assertNotEqual(self.dlg.edited_events(), original)
        self.dlg._on_undo()
        self.assertEqual(self.dlg.edited_events(), original)

    def test_apply_rejects_invalid_timeline(self):
        self.dlg._events = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"}
        ]
        self.dlg._on_apply()
        self.assertIn("未应用", self.dlg.status.text())

    def test_leading_wait_is_exposed_in_choices(self):
        # 任务书首版支持开头空等：界面不得再用 boundary > 0 过滤掉它。
        events = [
            {"t": 0.8, "kind": "key", "key": "a", "action": "down"},
            {"t": 1.0, "kind": "key", "key": "a", "action": "up"},
        ]
        choices = wait_gap_choices(events)
        self.assertTrue(choices)
        self.assertEqual(choices[0][0].boundary, 0)
        self.assertIn("开头空等", choices[0][1])
        # 开头空等可调整：缩短 0.5s 后整条事件流前移且内部时长不变
        from fukua_rpa.timeline_editor import adjust_wait

        result = adjust_wait(events, boundary=0, delta_seconds=-0.5)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual([e["t"] for e in result.events], [0.3, 0.5])

    def test_rejects_invalid_summary_from_apply(self):
        self.dlg._events = []
        self.dlg._on_apply()
        self.assertIn("未应用", self.dlg.status.text())


    def test_paired_releases_do_not_add_phantom_rows(self):
        """完整动作只占一行：单击与按键不再各多出一条「孤立的…松开」。"""
        dlg = TimelineEditorPanel(
            {
                "type": TASK_TYPE_INPUT_TIMELINE,
                "input_events": [
                    {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 10, "y": 20},
                    {"t": 0.1, "kind": "mouse", "button": "left", "action": "up", "x": 10, "y": 20},
                    {"t": 0.3, "kind": "key", "key": "a", "action": "down"},
                    {"t": 0.4, "kind": "key", "key": "a", "action": "up"},
                ],
                "playback_mode": "raw",
            }
        )
        self.addCleanup(self._dispose_extra, dlg)
        self.assertEqual(dlg.event_list.topLevelItemCount(), 2)
        texts = [
            dlg.event_list.topLevelItem(i).text(0) for i in range(dlg.event_list.topLevelItemCount())
        ]
        self.assertFalse(any("松开" in text for text in texts))

    def test_paired_repeats_do_not_add_phantom_rows(self):
        """带自动重复的按键只占一行：repeat 不再单列「孤立的按键」幻影。"""
        dlg = TimelineEditorPanel(
            {
                "type": TASK_TYPE_INPUT_TIMELINE,
                "input_events": [
                    {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
                    {"t": 0.1, "kind": "key", "key": "a", "action": "repeat"},
                    {"t": 0.2, "kind": "key", "key": "a", "action": "up"},
                ],
                "playback_mode": "raw",
            }
        )
        self.addCleanup(self._dispose_extra, dlg)
        self.assertEqual(dlg.event_list.topLevelItemCount(), 1)
        text = dlg.event_list.topLevelItem(0).text(0)
        self.assertIn("按键 a", text)
        self.assertIn("重复 1", text)
        for phantom in ("松开", "孤立", "repeat"):
            self.assertNotIn(phantom, text)

    def test_four_columns_and_row_chips(self):
        dlg = self.dlg
        self.assertEqual(dlg.event_list.columnCount(), 4)
        self.assertEqual(
            [dlg.event_list.headerItem().text(i) for i in range(4)],
            ["动作", "参数", "时间", "操作"],
        )
        from PySide6.QtWidgets import QPushButton

        from fukua_rpa.ui.timeline_editor_dialog import chip_rects, row_action_chips

        self.assertEqual(dlg.event_list.findChildren(QPushButton), [])
        for i in range(dlg.event_list.topLevelItemCount()):
            item = dlg.event_list.topLevelItem(i)
            chips = tuple(item.data(3, Qt.UserRole + 1) or ())
            row = dlg._row_by_id(item.data(0, Qt.UserRole))
            self.assertEqual(chips, row_action_chips(row.capability))
            self.assertTrue(chips)
            self.assertEqual(chips[0][0], "detail")
            cell = dlg.event_list.visualRect(dlg.event_list.model().index(i, 3))
            rects = [rect for _action, rect in chip_rects(cell, chips, dlg.event_list)]
            self.assertEqual(len(rects), len(chips))
            for rect in rects:
                self.assertTrue(cell.contains(rect))

    def test_row_action_targets_clicked_row(self):
        dlg = self.dlg
        count = dlg.event_list.topLevelItemCount()
        if count < 2:
            self.skipTest("需要至少两行")
        first = dlg.event_list.topLevelItem(0).data(0, Qt.UserRole)
        second = dlg.event_list.topLevelItem(1).data(0, Qt.UserRole)
        self._select_row(dlg, 0)
        dlg._on_row_action(second, "detail")
        self.assertEqual(dlg.event_list.currentItem().data(0, Qt.UserRole), second)
        self.assertEqual(dlg._selected_id, second)
        row = dlg._row_by_id(second)
        if "delete" in row.capability:
            dlg._on_row_action(second, "delete")
            self.assertIsNone(dlg._row_by_id(second))
            self.assertIsNotNone(dlg._row_by_id(first))

    def test_chip_click_target_conservation(self):
        """胶囊中心点击经 editorEvent → actionTriggered，多重集守恒。"""
        from collections import Counter

        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QStyleOptionViewItem

        from fukua_rpa.ui.timeline_editor_dialog import chip_rects

        dlg = self.dlg
        received = Counter()
        dlg.action_delegate.actionTriggered.connect(lambda rid, action: received.update([(rid, action)]))
        expected = Counter()
        for i in range(dlg.event_list.topLevelItemCount()):
            index = dlg.event_list.model().index(i, 3)
            item = dlg.event_list.topLevelItem(i)
            row_id = item.data(0, Qt.UserRole)
            chips = tuple(item.data(3, Qt.UserRole + 1) or ())
            option = QStyleOptionViewItem()
            option.rect = dlg.event_list.visualRect(index)
            for action, rect in chip_rects(option.rect, chips, dlg.event_list):
                expected.update([(row_id, action)])
                center = rect.center()
                press = QMouseEvent(QEvent.MouseButtonPress, center, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
                release = QMouseEvent(QEvent.MouseButtonRelease, center, Qt.LeftButton, Qt.MouseButton(0), Qt.NoModifier)
                dlg.action_delegate.editorEvent(press, dlg.event_list.model(), option, index)
                dlg.action_delegate.editorEvent(release, dlg.event_list.model(), option, index)
        self.assertEqual(received, expected)
        self.assertGreater(sum(expected.values()), 0)

    def test_column_width_relation(self):
        from PySide6.QtWidgets import QStyle

        from fukua_rpa.ui.timeline_editor_dialog import plan_column_widths

        dlg = self.dlg
        minimums = dlg._reference_minimums()
        desired = dict(minimums)
        extent = dlg.event_list.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        v0 = sum(minimums.values()) + extent
        for viewport in (v0 + 300, v0):
            widths, hscroll = plan_column_widths(viewport, desired, minimums, extent)
            self.assertFalse(hscroll)
            self.assertLessEqual(sum(widths), viewport)
            self.assertGreaterEqual(widths[3], minimums["op"])
        widths, hscroll = plan_column_widths(v0 - 1, desired, minimums, extent)
        self.assertTrue(hscroll)
        self.assertEqual(widths, (minimums["action"], minimums["param"], minimums["time"], minimums["op"]))

    def test_chip_height_is_not_a_hairline_in_both_themes(self):
        """行尾按钮高度由主题字体决定，浅色主题不得退化成 2px 细线。"""
        from fukua_rpa.ui import theme
        from fukua_rpa.ui.timeline_editor_dialog import ACTION_LABELS, chip_metrics

        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        chips = tuple((a, ACTION_LABELS[a]) for a in ("detail", "edit_pos", "delete"))
        for name, sheet in (("浅色", theme.APP_STYLESHEET), ("深色", theme.DARK_STYLESHEET)):
            with self.subTest(theme=name):
                self.app.setStyleSheet(sheet)
                _widths, height = chip_metrics(self.dlg.event_list, chips)
                # 返修前浅色主题仅 2px；修复后应至少达到字体行高量级（>=10px）。
                self.assertGreaterEqual(
                    height, self.dlg.event_list.fontMetrics().height(),
                    f"{name}主题按钮高度 {height}px 退化，绘制与命中区不可用",
                )
                # 绘制矩形与点击命中共用 chip_rects，高度同源
                from PySide6.QtCore import QRect

                from fukua_rpa.ui.timeline_editor_dialog import chip_rects
                rects = chip_rects(QRect(0, 0, 400, 40), chips, self.dlg.event_list)
                self.assertTrue(rects)
                for _action, rect in rects:
                    self.assertEqual(rect.height(), height)

    def test_time_column_not_omitted_and_never_below_minimums(self):
        """宽窗口时间列取完整文本宽度；任意预算下各列不低于下限。"""
        from PySide6.QtWidgets import QStyle

        from fukua_rpa.ui.timeline_editor_dialog import plan_column_widths

        dlg = self.dlg
        minimums = dlg._reference_minimums()
        extent = dlg.event_list.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        base = sum(minimums.values()) + extent

        # 宽文本：desired.time > min_time，时间列必须取完整宽度而非压到 min_time
        wide = dict(minimums)
        wide["time"] = minimums["time"] + 60
        widths, _hscroll = plan_column_widths(base + 400, wide, minimums, extent)
        self.assertEqual(widths[2], wide["time"], "宽文本时时间列应取完整 sizeHint")
        self.assertGreaterEqual(widths[0], minimums["action"])
        self.assertGreaterEqual(widths[1], minimums["param"])

        # 窄文本：desired.time < min_time，仍守住 min_time（不因文本窄而压缩）
        narrow = dict(minimums)
        narrow["time"] = minimums["time"] - 40
        widths_n, _hscroll = plan_column_widths(base + 400, narrow, minimums, extent)
        self.assertEqual(widths_n[2], minimums["time"], "窄文本时时间列仍不低于参考宽度")

        # 逐点扫描：宽/窄两组样本，任意预算下三列都不低于下限；宽文本中间区间走回退
        for desired in (wide, narrow):
            for viewport in range(base - 80, base + 400, 13):
                w, hscroll = plan_column_widths(viewport, desired, minimums, extent)
                self.assertGreaterEqual(w[0], minimums["action"])
                self.assertGreaterEqual(w[1], minimums["param"])
                self.assertGreaterEqual(w[2], max(minimums["time"], desired["time"]))
                if desired is wide and viewport < base + (wide["time"] - minimums["time"]):
                    self.assertTrue(hscroll, "中间预算区间应回退而非压缩参数列")

    def test_time_cells_distinguish_instants_and_duration(self):
        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "input_events": [
                {"t": 0.100, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.170, "kind": "key", "key": "a", "action": "up"},
                {"t": 0.270, "kind": "wheel", "delta": -120},
                {"t": 12.345, "kind": "key", "key": "b", "action": "down"},
                {"t": 13.456, "kind": "key", "key": "b", "action": "up"},
                {"t": 14.0001, "kind": "key", "key": "c", "action": "down"},
                {"t": 14.0002, "kind": "key", "key": "c", "action": "up"},
            ],
        }
        dlg = TimelineEditorPanel(task)
        self.addCleanup(dlg.close)
        tree = dlg.event_list
        self.assertEqual(
            [tree.topLevelItem(row).text(2) for row in range(tree.topLevelItemCount())],
            ["0.100–0.170 秒", "0.270 秒", "12.345–13.456 秒", "14.000–14.000 秒"],
        )
        self.assertEqual(dlg.edited_events(), task["input_events"])

    def test_time_column_updates_from_styled_cell_hint(self):
        from fukua_rpa.ui.theme import build_modern_stylesheet

        dlg = self.dlg
        dlg.resize(1428, 820)
        dlg.show()
        self.app.processEvents()
        tree = dlg.event_list
        time_index = tree.model().index(0, 2)
        self.assertTrue(time_index.isValid())

        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                # 让 Qt 的实际内边距与字体宽度估算不同，同时覆盖运行中换样式的路径。
                sheet = build_modern_stylesheet(1.0, theme)
                dlg.setStyleSheet(sheet + "\nQTreeWidget::item { padding: 4px 22px; }")
                self.app.processEvents()
                self.assertGreaterEqual(tree.columnWidth(2), tree.sizeHintForIndex(time_index).width())

    def test_vertical_scrollbar_does_not_add_hscroll(self):
        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "input_events": [
                item
                for i in range(120)
                for item in (
                    {"t": round(i * 0.01, 3), "kind": "key", "key": "a", "action": "down"},
                    {"t": round(i * 0.01 + 0.005, 3), "kind": "key", "key": "a", "action": "up"},
                )
            ],
            "playback_mode": "raw",
        }
        dlg = TimelineEditorPanel(task)
        self.addCleanup(self._dispose_extra, dlg)
        dlg.resize(1200, 700)
        dlg.show()
        self.app.processEvents()
        dlg._apply_column_widths()
        self.app.processEvents()
        self.assertGreater(dlg.event_list.topLevelItemCount(), 20)
        self.assertGreater(dlg.event_list.verticalScrollBar().maximum(), 0)
        self.assertEqual(dlg.event_list.horizontalScrollBar().maximum(), 0)

    def test_both_themes_style_tree_widget(self):
        from fukua_rpa.ui.theme import APP_STYLESHEET, DARK_STYLESHEET

        for sheet in (APP_STYLESHEET, DARK_STYLESHEET):
            for selector in ("QTreeWidget", "QTreeView::item", "QHeaderView::section"):
                self.assertIn(selector, sheet)

    def test_hover_moves_between_chips(self):
        if self.dlg.event_list.topLevelItemCount() < 2:
            self.skipTest("需要至少两行")
        from fukua_rpa.ui.timeline_editor_dialog import chip_rects

        dlg = self.dlg
        dlg.show()
        self.app.processEvents()
        for i in (0, 1):
            item = dlg.event_list.topLevelItem(i)
            cell = dlg.event_list.visualRect(dlg.event_list.model().index(i, 3))
            chips = tuple(item.data(3, Qt.UserRole + 1) or ())
            if not chips:
                continue
            rect = chip_rects(cell, chips, dlg.event_list)[0][1]
            dlg._on_hover(rect.center())
            self.assertEqual(dlg.action_delegate.hover[0], item.data(0, Qt.UserRole))

    def test_context_menu_targets_clicked_row(self):
        """右键真实事件：菜单按光标下行的 row_id 构建，删除光标行而非选中行。"""
        from PySide6.QtGui import QContextMenuEvent

        events = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.05, "kind": "key", "key": "a", "action": "up"},
            {"t": 0.2, "kind": "key", "key": "b", "action": "down"},
            {"t": 0.25, "kind": "key", "key": "b", "action": "up"},
            {"t": 0.4, "kind": "key", "key": "c", "action": "down"},
            {"t": 0.45, "kind": "key", "key": "c", "action": "up"},
        ]
        # 两种来源都要覆盖：原样回放（raw）与普通录制（无 playback_mode）
        for mode in ("raw", "normal"):
            with self.subTest(mode=mode):
                task = {"type": TASK_TYPE_INPUT_TIMELINE, "input_events": events}
                if mode == "raw":
                    task["playback_mode"] = "raw"
                dlg = TimelineEditorPanel(task)
                self.addCleanup(self._dispose_extra, dlg)
                dlg.resize(1200, 700)
                dlg.show()
                self.app.processEvents()

                first_label = dlg._display_rows[0].label
                second = dlg._display_rows[1].id  # B 的 row_id，用于断言菜单按键标构建
                second_label = dlg._display_rows[1].label
                self._select_row(dlg, 0)  # 选中 A
                self.app.processEvents()
                # 真实右键事件：pos 用视口坐标（visualRect 口径），不再手工 mapTo
                cell = dlg.event_list.visualRect(dlg.event_list.model().index(1, 0))
                pos = cell.center()

                class _FakeAction:
                    def __init__(self, text):
                        self._text = text
                        self._callback = None
                        self.triggered = self

                    def text(self):
                        return self._text

                    def setEnabled(self, _enabled):
                        pass

                    def connect(self, callback):
                        self._callback = callback

                    def trigger(self):
                        self._callback()

                class _FakeMenu:
                    def __init__(self, _parent=None, _dlg=dlg):
                        self._actions = []
                        _dlg._last_menu = self

                    def addAction(self, text):
                        action = _FakeAction(text)
                        self._actions.append(action)
                        return action

                    def actions(self):
                        return list(self._actions)

                    def exec(self, _point=None):
                        return None

                # spy：记录实现真正传给 _on_row_action 的 (row_id, action)，并保留真实删除
                dispatched = []
                real_handler = dlg._on_row_action

                def spy(rid, act, _dispatched=dispatched, _real=real_handler):
                    _dispatched.append((rid, act))
                    return _real(rid, act)

                with mock.patch("fukua_rpa.ui.timeline_editor_dialog.QMenu", _FakeMenu), mock.patch.object(
                    dlg, "_on_row_action", spy
                ):
                    ev = QContextMenuEvent(
                        QContextMenuEvent.Mouse, pos, dlg.event_list.viewport().mapToGlobal(pos)
                    )
                    # 直接驱动 Qt 右键时调用的处理函数入口（等价于真实右键送达）
                    dlg.event_list.contextMenuEvent(ev)
                    self.app.processEvents()

                    menus = getattr(dlg, "_last_menu", None)
                    self.assertIsNotNone(menus, "右键事件应触发菜单构建")
                    # 在 patch 生效期间点击「删除」，回调经 spy 记录真实 row_id
                    triggered = False
                    for action in menus.actions():
                        if action.text() == "删除":
                            action.trigger()
                            triggered = True
                            break
                    self.assertTrue(triggered, "菜单应含「删除」动作")

                # 菜单必须按光标下 B 的 row_id 构建，不是选中行 A
                self.assertIn(
                    (second, "delete"), dispatched, f"删除应作用于光标行 B({second})，实际 {dispatched}"
                )
                first_id = dlg._display_rows[0].id
                self.assertNotIn((first_id, "delete"), dispatched, "不得作用于选中行 A")
                self.assertEqual(dispatched.count((second, "delete")), 1)
                # 按 label 判断（B 的 id 会被 C 复用）
                labels_after = [row.label for row in dlg._display_rows]
                self.assertNotIn(second_label, labels_after, "B 应被删除")
                self.assertIn(first_label, labels_after, "A 应保留")

    def test_delete_key_keeps_detail_in_sync_with_list(self):
        """删除选中动作后，列表 current 与详情指向同一行；撤销恢复全部事件。"""
        from PySide6.QtGui import QKeyEvent

        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "playback_mode": "raw",
            "input_events": [
                {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.05, "kind": "key", "key": "a", "action": "up"},
                {"t": 0.2, "kind": "key", "key": "b", "action": "down"},
                {"t": 0.25, "kind": "key", "key": "b", "action": "up"},
                {"t": 0.4, "kind": "key", "key": "c", "action": "down"},
                {"t": 0.45, "kind": "key", "key": "c", "action": "up"},
            ],
        }
        dlg = TimelineEditorPanel(task)
        self.addCleanup(self._dispose_extra, dlg)
        dlg.resize(1200, 700)
        dlg.show()
        self.app.processEvents()

        self.assertEqual(len(dlg._display_rows), 3)
        rows = list(dlg._display_rows)
        b_label = rows[1].label  # 按键 id 按事件序号生成，删除后会被复用，故用 label 识别
        # 真实选中 B：触发 currentItemChanged → 详情显示 B
        self._select_row(dlg, 1)
        self.app.processEvents()
        self.assertIn(b_label, dlg.detail.text())
        original_events = len(dlg._events)

        # 按 Delete 删除 B
        dlg.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        self.app.processEvents()
        labels_after = [row.label for row in dlg._display_rows]
        self.assertNotIn(b_label, labels_after, "B 应被删除（id 会被 C 复用，按 label 判断）")
        self.assertEqual(len(labels_after), 2)
        current = dlg.event_list.currentItem()
        self.assertIsNotNone(current)
        current_id = current.data(0, Qt.UserRole)
        self.assertEqual(current_id, dlg._selected_id)
        # 详情必须跟随列表 current（纯 label，不带序号前缀），不再停留 B
        current_row = dlg._row_by_id(current_id)
        self.assertIsNotNone(current_row)
        self.assertIn(current_row.label, dlg.detail.text())
        self.assertNotIn(b_label, dlg.detail.text())

        # 撤销恢复全部事件
        dlg._on_undo()
        self.app.processEvents()
        self.assertEqual(len(dlg._events), original_events)
        self.assertEqual(len(dlg._display_rows), 3)
        self.assertIn(b_label, [row.label for row in dlg._display_rows])

    def test_middle_gap_keeps_global_wait_entry(self):
        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "input_events": [
                {"t": 0.5, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.6, "kind": "key", "key": "a", "action": "up"},
                {"t": 1.2, "kind": "key", "key": "b", "action": "down"},
                {"t": 1.3, "kind": "key", "key": "b", "action": "up"},
            ],
            "playback_mode": "raw",
        }
        dlg = TimelineEditorPanel(task)
        self.addCleanup(self._dispose_extra, dlg)
        self.assertTrue(dlg.edit_wait_btn.isEnabled())
        self.assertTrue(any(gap.boundary != 0 for gap, _label in wait_gap_choices(dlg._events)))

    def _long_dialog(self):
        events = []
        for i in range(80):
            events.append({"t": round(i * 0.01, 3), "kind": "move", "x": 100 + i, "y": 200})
        events.insert(0, {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 100, "y": 200})
        events.append({"t": 0.8, "kind": "mouse", "button": "left", "action": "up", "x": 180, "y": 200})
        dlg = TimelineEditorPanel({
            "type": TASK_TYPE_INPUT_TIMELINE,
            "input_events": events,
            "recorded_duration": 0.8,
            "playback_mode": "raw",
        })
        self.addCleanup(self._dispose_extra, dlg)
        dlg.resize(1428, 915)
        dlg.show()
        self.app.processEvents()
        return dlg

    def test_default_layout_list_is_full_width_and_detail_collapsed(self):
        """打开即整宽列表占主区域，详情默认收起（对照基线半屏留白/右侧挤压）。"""
        dlg = self._long_dialog()
        self.assertFalse(dlg.detail_panel.isVisible(), "详情默认应收起")
        # 列表几乎占满客户区宽度，且高度显著大于基线被挤压的 368px。
        self.assertGreater(dlg.event_list.viewport().width(), dlg.width() - 60)
        self.assertGreater(dlg.event_list.viewport().height(), 600)
        # 顶部信息条与调整等待入口同处 header，标题区不制造大块留白。
        self.assertGreater(dlg.event_list.height(), dlg.summary_label.height() * 10)

    def test_selection_updates_content_but_keeps_detail_collapsed(self):
        """选中行只更新待查看内容，不自动展开详情。"""
        dlg = self._long_dialog()
        self._select_row(dlg, 0)
        self.assertFalse(dlg.detail_panel.isVisible(), "选中行不应自动展开详情")
        self.assertIn("→", dlg.detail.text(), "详情文本应随选中行更新")

    def test_detail_toggle_below_list_keeps_same_width_and_scrollable(self):
        """点击详情在列表下方展开：列表保持整宽且仍有可滚动高度；再次点击收起。"""
        dlg = self._long_dialog()
        width_before = dlg.event_list.viewport().width()
        height_before = dlg.event_list.viewport().height()
        row_id = dlg.event_list.topLevelItem(0).data(0, Qt.UserRole)
        dlg._on_row_action(row_id, "detail")
        self.app.processEvents()
        self.assertTrue(dlg.detail_panel.isVisible(), "详情应在列表下方展开")
        self.assertEqual(dlg.event_list.viewport().width(), width_before, "展开后列表宽度不变")
        self.assertGreater(dlg.event_list.viewport().height(), 200, "展开后列表仍应有可滚动高度")
        self.assertLess(dlg.event_list.viewport().height(), height_before, "详情占用了部分高度")
        # 详情与列表同宽
        self.assertEqual(dlg.detail_panel.width(), dlg.event_list.width())
        # 改选其他行时详情内容同步更新
        if dlg.event_list.topLevelItemCount() > 1:
            second_id = dlg.event_list.topLevelItem(1).data(0, Qt.UserRole)
            dlg._on_row_action(second_id, "detail")
            self.app.processEvents()
            # 点击第二行详情是 toggle：面板收起；再点一次展开并指向新行
            if not dlg.detail_panel.isVisible():
                dlg._on_row_action(second_id, "detail")
                self.app.processEvents()
            self.assertIn(dlg._row_by_id(second_id).label, dlg.detail.text())
        # 再次点击当前详情行应收起
        dlg._on_row_action(row_id, "detail")
        self.app.processEvents()
        self.assertFalse(dlg.detail_panel.isVisible(), "再次点击详情应收起")

    def test_detail_toggle_uses_clicked_row_not_selection(self):
        """行尾详情动作按 row_id 命中行，不依赖当前选中行。"""
        dlg = self._long_dialog()
        count = dlg.event_list.topLevelItemCount()
        if count < 2:
            self.skipTest("需要至少两行")
        first = dlg.event_list.topLevelItem(0).data(0, Qt.UserRole)
        second = dlg.event_list.topLevelItem(1).data(0, Qt.UserRole)
        self._select_row(dlg, 0)
        dlg._on_row_action(second, "detail")
        self.app.processEvents()
        self.assertEqual(dlg.event_list.currentItem().data(0, Qt.UserRole), second)
        self.assertIn(dlg._row_by_id(second).label, dlg.detail.text())
        self.assertNotEqual(first, second)

    def _dispose_extra(self, dlg):
        dlg.close()
        dlg.deleteLater()
        self.app.processEvents()


class TimelineEditorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.window = RPAWindow(base_dir=temporary.name, defer_runtime=True)
        self.addCleanup(self._dispose)
        self.task_list = self.window.task_list
        self.controller = self.window.workflow_editor
        self.window.show()
        self.app.processEvents()

    def _dispose(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def _row(self, index):
        return self.window.ensure_task_item_widget(self.task_list.item(index))

    def _open(self, index):
        row = self._row(index)
        row.timeline_edit_btn.click()
        self.app.processEvents()
        self.assertIsNotNone(row.timeline_editor)
        return row.timeline_editor

    def _edited_move(self, panel):
        events = panel.edited_events()
        events.pop(1)  # 保留鼠标按下、松开和其余采样
        panel._events = events
        return events

    def _click(self, panel, object_name):
        panel.findChild(QPushButton, object_name).click()
        self.app.processEvents()

    def test_identifies_only_raw_timeline_tasks(self):
        self.assertTrue(is_raw_timeline_task(_raw_task()))
        self.assertFalse(is_raw_timeline_task(dict(_raw_task(), playback_mode="")))
        self.assertFalse(is_raw_timeline_task({"type": 1.0}))

    def test_row_button_embeds_editor_below_step_in_main_window(self):
        self.window.restore_task_snapshot([_raw_task(), {"type": 1.0}])
        row = self._row(0)
        self.assertTrue(self._row(1).timeline_edit_btn.isHidden())
        self.assertTrue(row.timeline_edit_btn.isVisible())
        visible_windows = {widget for widget in self.app.topLevelWidgets() if widget.isVisible()}
        QTest.mouseClick(row.timeline_edit_btn, Qt.LeftButton)
        self.app.processEvents()
        panel = row.timeline_editor
        self.assertIsNotNone(panel)
        self.assertTrue(panel.isVisible())
        self.assertFalse(panel.isWindow())
        self.assertIs(panel.parentWidget(), row)
        self.assertIs(panel.window(), self.window)
        self.assertGreater(panel.geometry().top(), row.controls_widget.geometry().bottom())
        self.assertEqual(self.task_list.count(), 2)
        self.assertEqual(
            {widget for widget in self.app.topLevelWidgets() if widget.isVisible()},
            visible_windows,
        )

    def test_context_menu_opens_row_under_the_cursor(self):
        self.window.restore_task_snapshot([_raw_task(), dict(_raw_task(), playback_mode="triggered")])
        self.task_list.setCurrentRow(0)
        viewport = self.task_list.viewport()
        point = self.task_list.visualItemRect(self.task_list.item(1)).center()

        class TestMenu(QMenu):
            def exec(self, *_):
                action = next(action for action in self.actions() if action.text() == "编辑录制")
                action.trigger()

        with mock.patch("fukua_rpa.ui.controllers.workflow_context.QMenu", TestMenu):
            event = QContextMenuEvent(QContextMenuEvent.Mouse, point, viewport.mapToGlobal(point))
            self.app.sendEvent(viewport, event)
        self.assertIsNone(self._row(0).timeline_editor)
        self.assertIsNotNone(self._row(1).timeline_editor)

    def test_switch_cancel_and_unchanged_apply_leave_workflow_intact(self):
        self.window.restore_task_snapshot([_raw_task(), _raw_task()])
        before = self.window.snapshot_tasks()
        history = len(self.controller.undo_stack)
        first = self._open(0)
        self.assertIs(self._open(0), first)
        second = self._open(1)
        self.assertIsNone(self._row(0).timeline_editor)
        self._edited_move(second)
        with mock.patch("fukua_rpa.ui.controllers.workflow_timeline_edit.QToolTip.showText"):
            self.assertFalse(self.controller.edit_timeline_recording(0))
        self.assertIs(self._row(1).timeline_editor, second)
        self._click(second, "timelineCancel")
        self.assertIsNone(self._row(1).timeline_editor)
        self._click(self._open(0), "timelineApply")
        self.assertIsNone(self._row(0).timeline_editor)
        self.assertEqual(self.window.snapshot_tasks(), before)
        self.assertEqual(len(self.controller.undo_stack), history)

    def test_apply_preserves_source_and_commits_once_with_one_undo(self):
        for mode in ("raw", "triggered"):
            with self.subTest(mode=mode):
                self.window.restore_task_snapshot([dict(_raw_task(), playback_mode=mode)])
                before = self.window.snapshot_tasks()
                history = len(self.controller.undo_stack)
                panel = self._open(0)
                edited = self._edited_move(panel)
                self._click(panel, "timelineApply")
                result = self.window.snapshot_tasks()[0]
                self.assertEqual(result["input_events"], edited)
                self.assertEqual(result["playback_mode"], mode)
                self.assertIn("41 个事件", result["value"])
                self.assertEqual(result["recorded_duration"], 0.32)
                self.assertEqual(len(self.controller.undo_stack), history + 1)
                self.assertIsNone(self._row(0).timeline_editor)
                self.controller.undo_task_change()
                self.assertEqual(self.window.snapshot_tasks(), before)

    def test_runtime_and_start_in_progress_reject_writeback(self):
        self.window.restore_task_snapshot([_raw_task()])
        panel = self._open(0)
        edited = self._edited_move(panel)
        before = self.window.snapshot_tasks()
        history = len(self.controller.undo_stack)
        execution = self.window.execution_controller
        execution.set_runtime_editing_locked(True)
        try:
            self.assertFalse(panel.isEnabled())
            self.assertFalse(self._row(0).timeline_edit_btn.isEnabled())
            panel.applyRequested.emit(edited)
        finally:
            execution.set_runtime_editing_locked(False)
        self.assertTrue(panel.isEnabled())
        self.assertTrue(self._row(0).timeline_edit_btn.isEnabled())
        execution.start_in_progress = True
        try:
            with mock.patch("fukua_rpa.ui.controllers.workflow_timeline_edit.QToolTip.showText"):
                self.assertFalse(self.controller.edit_timeline_recording(0))
            self._click(panel, "timelineApply")
        finally:
            execution.start_in_progress = False
        self.assertEqual(self.window.snapshot_tasks(), before)
        self.assertEqual(len(self.controller.undo_stack), history)
        self._click(panel, "timelineApply")
        self.assertEqual(self.window.snapshot_tasks()[0]["input_events"], edited)

    def test_overwritten_payload_refuses_apply_and_keeps_draft(self):
        self.window.restore_task_snapshot([_raw_task()])
        panel = self._open(0)
        edited = self._edited_move(panel)
        self._row(0).custom_data["input_events"][2]["x"] += 1
        before = self.window.snapshot_tasks()
        history = len(self.controller.undo_stack)
        self._click(panel, "timelineApply")
        self.assertIn("原步骤已变化", panel.status.text())
        self.assertEqual(panel.edited_events(), edited)
        self.assertEqual(self.window.snapshot_tasks(), before)
        self.assertEqual(len(self.controller.undo_stack), history)


    def test_collapsing_group_preserves_draft_in_header_and_member_rows(self):
        group = {"id": "recording", "name": "录制步骤", "collapsed": False}
        for index in (0, 1):
            with self.subTest(index=index):
                self.window.restore_task_snapshot([dict(_raw_task(), editor_groups=[group]) for _ in range(2)])
                panel = self._open(index)
                edited = self._edited_move(panel)
                toggle = self._row(0).group_toggle_buttons[0]
                toggle.click()
                self.app.processEvents()
                self.assertFalse(panel.isVisible())
                toggle.click()
                self.app.processEvents()
                self.assertTrue(panel.isVisible())
                self.assertIs(self._row(index).timeline_editor, panel)
                self.assertEqual(panel.edited_events(), edited)
                self.assertEqual(self.task_list.count(), 2)
                self._click(panel, "timelineCancel")

    def test_inline_detail_expands_row_and_keeps_both_canvases_visible(self):
        self.window.restore_task_snapshot([_raw_task()])
        item = self.task_list.item(0)
        panel = self._open(0)
        collapsed_height = item.sizeHint().height()
        row_id = panel.event_list.topLevelItem(0).data(0, Qt.UserRole)
        panel._on_row_action(row_id, "detail")
        # 子布局请求、外层 item 提示更新、列表排版由 Qt 分轮投递。
        for _ in range(4):
            self.app.processEvents()
        self.assertGreater(item.sizeHint().height(), collapsed_height)
        self.assertGreaterEqual(panel.track.height(), panel.track.minimumSizeHint().height())
        self.assertGreaterEqual(panel.preview.height(), panel.preview.minimumSizeHint().height())
        self.assertGreaterEqual(panel.event_list.height(), panel.event_list.minimumSizeHint().height())
        panel._on_row_action(row_id, "detail")
        for _ in range(4):
            self.app.processEvents()
        self.assertEqual(item.sizeHint().height(), collapsed_height)

    def test_virtualization_releases_active_row_only_after_closing_editor(self):
        count = self.task_list.VIRTUALIZATION_THRESHOLD + 45
        ordinary = [{"type": 1.0, "value": str(index)} for index in range(count)]
        self.window.restore_task_snapshot([_raw_task(), *ordinary])
        original_scroll_mode = self.task_list.verticalScrollMode()
        item = self.task_list.item(0)
        panel = self._open(0)
        edited = self._edited_move(panel)
        self.task_list.setCurrentRow(self.task_list.count() - 1)
        self.task_list.scrollToBottom()
        self.window.log_text.setFocus()
        self.task_list.refresh_virtual_widgets()
        self.assertLess(self.task_list.visualItemRect(item).bottom(), 0)
        self.assertIs(self.task_list.itemWidget(item).timeline_editor, panel)
        self.assertEqual(panel.edited_events(), edited)
        self.assertLess(self.task_list.materialized_row_count(), self.task_list.count())
        self._click(panel, "timelineCancel")
        self.task_list.refresh_virtual_widgets()
        self.assertIsNone(self.task_list.itemWidget(item))
        self.assertEqual(self.task_list.verticalScrollMode(), original_scroll_mode)

    def test_editor_shortcuts_do_not_change_outer_workflow(self):
        key_events = [
            {"t": i / 10, "kind": "key", "key": key, "action": action}
            for i, (key, action) in enumerate(
                (key, action) for key in "abc" for action in ("down", "up")
            )
        ]
        self.window.restore_task_snapshot([dict(_raw_task(), input_events=key_events), {"type": 1.0}])
        self.task_list.setCurrentRow(0)
        before = self.window.snapshot_tasks()
        history = len(self.controller.undo_stack)
        event_list = self._open(0).event_list
        event_list.setCurrentItem(event_list.topLevelItem(1))
        QTest.keyClick(event_list, Qt.Key_Delete)
        self.assertNotIn("b", {event["key"] for event in self._row(0).timeline_editor.edited_events()})
        QTest.keyClick(event_list, Qt.Key_Z, Qt.ControlModifier)
        self.assertEqual(self._row(0).timeline_editor.edited_events(), key_events)
        with mock.patch.object(self._row(0), "open_custom_config") as step_settings:
            for key in (Qt.Key_Y, Qt.Key_C, Qt.Key_X, Qt.Key_V, Qt.Key_D, Qt.Key_Up, Qt.Key_Down):
                QTest.keyClick(event_list, key, Qt.ControlModifier)
            for key in (Qt.Key_Insert, Qt.Key_F2, Qt.Key_Return, Qt.Key_F8):
                QTest.keyClick(event_list, key)
            step_settings.assert_not_called()
        self.assertEqual(self.window.snapshot_tasks(), before)
        self.assertEqual(len(self.controller.undo_stack), history)
        QTest.keyClick(event_list, Qt.Key_Escape)
        self.assertIsNone(self._row(0).timeline_editor)
        QTest.keyClick(self.task_list, Qt.Key_Down, Qt.ControlModifier)
        self.assertEqual(self.window.snapshot_tasks()[1]["step_id"], before[0]["step_id"])

    def _move_below_next(self, index, *, drag):
        self.task_list.setCurrentRow(index)
        if drag:
            self.task_list.scrollToTop()
            self.app.processEvents()
            target = self.task_list.visualItemRect(self.task_list.item(index + 1))
            dest = QPoint(target.center().x(), target.bottom() - 2)
            self.task_list._begin_manual_drag(index)
            self.assertTrue(self.task_list._finish_manual_drag(commit=True, pos=dest))
        else:
            self.assertTrue(self.window.move_selected_task(1))

    def test_moving_active_step_closes_and_disarms_old_editor(self):
        for drag in (False, True):
            with self.subTest(drag=drag):
                self.window.restore_task_snapshot([_raw_task(), {"type": 1.0}])
                panel = self._open(0)
                edited = self._edited_move(panel)
                anchor = self.window.snapshot_tasks()[0]["step_id"]
                self._move_below_next(0, drag=drag)
                self.assertFalse(panel.isVisible())
                before = self.window.snapshot_tasks()
                panel.applyRequested.emit(edited)
                self.app.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()
                self.assertFalse(widget_is_alive(panel))
                self.assertEqual(self.window.snapshot_tasks(), before)
                self.assertEqual(before[1]["step_id"], anchor)
                self.assertIsNone(self.task_list.active_timeline_editor_item())

    def test_other_step_crossing_anchor_keeps_draft_at_its_actual_item(self):
        for drag in (False, True):
            with self.subTest(drag=drag):
                self.window.restore_task_snapshot([{"type": 1.0}, _raw_task()])
                panel = self._open(1)
                edited = self._edited_move(panel)
                anchor = self.window.snapshot_tasks()[1]["step_id"]
                self._move_below_next(0, drag=drag)
                self.assertIs(self._row(0).timeline_editor, panel)
                self._click(panel, "timelineApply")
                current = self.window.snapshot_tasks()
                self.assertEqual(current[0]["step_id"], anchor)
                self.assertEqual(current[0]["input_events"], edited)

    def test_delete_and_profile_change_disarm_old_editor(self):
        for switch_profile in (False, True):
            with self.subTest(switch_profile=switch_profile):
                self.window.restore_task_snapshot([_raw_task(), {"type": 1.0}])
                panel = self._open(0)
                edited = self._edited_move(panel)
                if switch_profile:
                    same_task = self.window.snapshot_tasks()[0]
                    destination = self.window.get_current_ui_config()
                    destination["tasks"] = [same_task]
                    self.window.profiles_data["目标方案"] = destination
                    self.window.profile_controller.on_profile_changed("目标方案")
                    self.assertEqual(self.window.current_profile_name, "目标方案")
                    self.assertEqual(self.window.snapshot_tasks(), [same_task])
                else:
                    self.assertTrue(self.window.del_row(self._row(0)))
                before = self.window.snapshot_tasks()
                if widget_is_alive(panel):
                    panel.applyRequested.emit(edited)
                self.app.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()
                self.assertFalse(widget_is_alive(panel))
                self.assertIsNone(self.task_list.active_timeline_editor_item())
                self.assertEqual(self.window.snapshot_tasks(), before)


if __name__ == "__main__":
    unittest.main()
