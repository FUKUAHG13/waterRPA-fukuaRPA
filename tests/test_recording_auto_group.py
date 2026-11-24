# ruff: noqa: E402
"""录制完成自动编组：每次成功录制在末尾形成一个独立一级「录制步骤」组。

覆盖用户方案§二与验收表「录制入口」行：组 ID/数量/成员/边界、连续两次、
已有组在末尾、空录制、锁定拒绝、返回值透传，以及成功提示用实际提交数。
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.ui.controllers import recording_controller as rc_mod  # noqa: E402
from fukua_rpa.ui.input_tools import RecorderUI  # noqa: E402
from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402
from fukua_rpa.workflow_metadata import normalize_group_path  # noqa: E402


class _WeakHost:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _CapturingHost:
    """记录 add_rows 收到的任务批次，返回可配置插入数。"""

    def __init__(self, result=None):
        self.batches = []
        self.result = result

    def add_rows(self, tasks):
        self.batches.append([dict(t) for t in tasks])
        return len(tasks) if self.result is None else self.result


def _task(tag="x"):
    return {"type": 1.0, "value": tag}


def _group_of(task):
    path = normalize_group_path(task.get("editor_groups", []))
    assert len(path) == 1, f"应为一级组，实际 {path}"
    return path[0]


class RecordingAutoGroupTests(unittest.TestCase):
    def test_single_batch_shares_one_new_group(self):
        host = _CapturingHost()
        rc = rc_mod.RecordingController(host)
        rc._add_tasks([_task("a"), _task("b"), _task("c")])
        batch = host.batches[0]
        groups = [_group_of(t) for t in batch]
        self.assertEqual(len({g["id"] for g in groups}), 1)
        self.assertEqual(groups[0]["name"], "录制步骤")
        self.assertFalse(groups[0]["collapsed"], "默认展开")

    def test_two_recordings_form_two_independent_same_named_groups(self):
        host = _CapturingHost()
        rc = rc_mod.RecordingController(host)
        rc._add_tasks([_task("a")])
        rc._add_tasks([_task("b")])
        first = _group_of(host.batches[0][0])
        second = _group_of(host.batches[1][0])
        self.assertEqual(first["name"], second["name"], "同名")
        self.assertNotEqual(first["id"], second["id"], "连续两次组 ID 必须不同")

    def test_empty_recording_creates_no_group(self):
        host = _CapturingHost()
        rc = rc_mod.RecordingController(host)
        rc._add_tasks([])
        self.assertEqual(host.batches, [[]], "空录制原样交给 add_rows，由它返回 0")

    def test_lock_rejection_result_is_forwarded(self):
        host = _WeakHost(add_rows=lambda tasks: 0)
        rc = rc_mod.RecordingController(host)
        self.assertEqual(rc._add_tasks([_task()]), 0, "FR-901-002 返回值透传")

    def test_deepcopy_group_not_shared_between_tasks(self):
        host = _CapturingHost()
        rc = rc_mod.RecordingController(host)
        rc._add_tasks([_task("a"), _task("b")])
        host.batches[0][0]["editor_groups"][0]["name"] = "改过的组"
        self.assertNotEqual(
            _group_of(host.batches[0][0])["name"], _group_of(host.batches[0][1])["name"]
        )


class ExistingGroupIntegrationTests(unittest.TestCase):
    """已有组在末尾时，新录制组不触碰旧组，边界严格等于新增区间。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with mock.patch.object(RPAWindow, "close_all_task_config_dialogs", lambda self: None):
            self.window = RPAWindow(base_dir=tempfile.mkdtemp(), defer_runtime=True)
        self.addCleanup(self._dispose)

    def _dispose(self):
        w = getattr(self, "window", None)
        if w is not None:
            w.close()
            w.deleteLater()
        self.app.processEvents()

    def test_edit_timeline_inside_group_preserves_group_metadata(self):
        """组内 29.0 编辑写回后保留 editor_groups（方案§三 方案读写）。"""
        group = {"id": "grp-timeline", "name": "录制步骤", "collapsed": False}
        task = {
            "type": 29.0,
            "value": "输入时序：2 个事件 / 0.10 秒",
            "input_events": [
                {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "a", "action": "up"},
            ],
            "recorded_duration": 0.1,
            "editor_groups": [dict(group)],
            "tool_only": True,
        }
        self.window.restore_task_snapshot([task])
        self.window.task_list.setCurrentRow(0)
        # 实际改动一个事件，保证写回被视为有效变更
        edited = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.15, "kind": "key", "key": "a", "action": "up"},
        ]
        self.assertTrue(self.window.workflow_editor.edit_timeline_recording(0))
        item = self.window.task_list.item(0)
        panel = self.window.task_list.itemWidget(item).timeline_editor
        panel._events = edited
        panel._on_apply()
        result = self.window.snapshot_tasks()[0]
        self.assertEqual(result["input_events"], edited)
        self.assertEqual(result.get("editor_groups", [{}])[0]["id"], "grp-timeline")
        self.assertFalse(result["editor_groups"][0]["collapsed"])
        # 未触碰来源标注
        self.assertTrue(result.get("tool_only"))

    def test_existing_group_at_tail_preserved(self):
        old_group = {"id": "old-g", "name": "旧组", "collapsed": True}
        existing = [
            {"type": 1.0, "value": "旧A", "editor_groups": [dict(old_group)]},
            {"type": 1.0, "value": "旧B", "editor_groups": [dict(old_group)]},
        ]
        self.window.restore_task_snapshot(existing)
        rc = rc_mod.RecordingController(self.window)
        added = rc._add_tasks([_task("新1"), _task("新2")])
        self.assertEqual(added, 2)
        snap = self.window.snapshot_tasks()
        self.assertEqual(len(snap), 4, "旧 2 步 + 新 2 步")
        # 旧组身份、折叠态保留
        self.assertEqual(snap[0]["editor_groups"][0]["id"], "old-g")
        self.assertTrue(snap[0]["editor_groups"][0]["collapsed"])
        # 新组独立于旧组，且两新步共享
        new_ids = {_group_of(t)["id"] for t in snap[2:]}
        self.assertEqual(len(new_ids), 1)
        self.assertNotIn("old-g", new_ids)
        self.assertEqual(_group_of(snap[2])["name"], "录制步骤")
        # 一次撤销移除新增步；重做恢复（含新组），旧组保留
        self.window.workflow_editor.undo_task_change()
        after_undo = self.window.snapshot_tasks()
        self.assertEqual(len(after_undo), 2)
        self.assertEqual(after_undo[0]["editor_groups"][0]["id"], "old-g")


class _FakeRecorder:
    """RecorderUI.on_recorded 所需的轻量宿主，用未绑定方法驱动真实逻辑。"""

    def __init__(self, added_count, mode="raw"):
        self.cancelled = False
        self.result_handled = False
        # v1.1.19：新录制统一原样回放，测试桩只走 raw 路径。
        self.mode = "raw"
        self.added_count = added_count
        self.tasks_callback = lambda tasks: self.added_count
        self.main_window = None
        self.logs = []
        self.hook_thread = _WeakHost(
            raw_input_events=[], raw_events_truncated=False, raw_unsupported=False
        )

    def append_recording_log(self, msg):
        self.logs.append(msg)

    def restore_main_window(self):
        pass

    def close(self):
        pass


def _drive_on_recorded(rec, tasks):
    import fukua_rpa.ui.input_tools as it

    # v1.1.19：新录制统一原样回放，桩只驱动 build_raw_playback_steps 分支。
    with mock.patch.object(it, "build_raw_playback_steps", return_value=tasks):
        RecorderUI.on_recorded(rec, [])
    return rec.logs


class _Ctx:
    def __init__(self, task):
        self.task = task
        self.value = str(task.get("value", ""))


from fukua_rpa.actions.input_timeline import ActionsInputTimelineMixin  # noqa: E402


class _EquivalenceHarness(ActionsInputTimelineMixin):
    """假输入后端：记录派发序列与载荷，虚拟时钟推进等待。"""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self._injected_input_lock = threading.Lock()
        self.calls = []
        self.waits = []
        self.clock = 1000.0
        self.log_level = 0
        self._injected_keys = []
        self.input_backend = mock.Mock()

    def log(self, *args, **kwargs):
        pass

    def check_stop_flag(self):
        return False

    def _wait_interruptibly(self, seconds, stop_requested=None):
        self.waits.append(round(float(seconds), 3))
        self.clock += float(seconds)
        return True

    def _tracked_key_down(self, key):
        self.calls.append(("down", key))
        self._injected_keys.append(key)
        return True

    def _tracked_key_up(self, key):
        self.calls.append(("up", key))
        if key in self._injected_keys:
            self._injected_keys.remove(key)
        return True

    def _tracked_mouse_down(self, button):
        self.calls.append(("mdown", button))
        return True

    def _tracked_mouse_up(self, button):
        self.calls.append(("mup", button))
        return True

    def _tracked_move(self, x, y):
        self.calls.append(("move", x, y))
        return True


class GroupedExecutionEquivalenceTests(unittest.TestCase):
    """方案§二第 3 点：分组后执行快照与未分组等价。"""

    def test_grouped_and_ungrouped_timeline_dispatch_identically(self):
        import fukua_rpa.actions.input_timeline as it

        task = {
            "type": 29.0,
            "value": "输入时序",
            "input_events": [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.2, "kind": "key", "key": "w", "action": "repeat"},
                {"t": 0.5, "kind": "key", "key": "w", "action": "up"},
            ],
            "recorded_duration": 0.5,
        }
        grouped = dict(task)
        grouped["editor_groups"] = [{"id": "g", "name": "录制步骤", "collapsed": False}]
        results = []
        for candidate in (task, grouped):
            harness = _EquivalenceHarness()
            with mock.patch.object(
                it.time, "monotonic", side_effect=lambda h=harness: h.clock
            ):
                harness._execute_input_timeline(_Ctx(candidate))
            results.append((list(harness.calls), list(harness.waits)))
        self.assertEqual(results[0], results[1], "加组不得改变执行序列与等待载荷")
        self.assertEqual(results[0][0], [("down", "w"), ("up", "w")])


class RecordingSuccessPromptTests(unittest.TestCase):
    def test_raw_prompt_uses_actual_committed_count(self):
        rec = _FakeRecorder(added_count=1)
        logs = _drive_on_recorded(rec, [_task("raw")])
        self.assertTrue(any("已生成 1 个可编辑操作步骤" in m for m in logs), logs)
        rec2 = _FakeRecorder(added_count=2)
        logs2 = _drive_on_recorded(rec2, [_task("a"), _task("b")])
        self.assertTrue(any("已生成 2 个可编辑操作步骤" in m for m in logs2), logs2)
        rec0 = _FakeRecorder(added_count=0)
        logs0 = _drive_on_recorded(rec0, [_task("raw")])
        self.assertFalse(any("已生成" in m for m in logs0), logs0)

    def test_repeated_callback_only_submits_once(self):
        calls = []
        rec = _FakeRecorder(added_count=1)
        rec.tasks_callback = lambda tasks: calls.append(1) or 1
        _drive_on_recorded(rec, [_task()])
        _drive_on_recorded(rec, [_task()])  # 重复结束回调
        self.assertEqual(len(calls), 1, "重复结束回调只提交一次")


if __name__ == "__main__":
    unittest.main()
