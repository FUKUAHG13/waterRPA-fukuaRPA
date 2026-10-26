"""FR-901-001/002 回归测试。

FR-901-001: 程序化切换方案（导入/新建/删除/恢复备份）后必须清空撤销/重做栈，
防止 Ctrl+Z 把旧方案步骤覆盖到当前方案并经自动保存落盘。
FR-901-002: 录制结果被运行期编辑锁丢弃时，_add_tasks 必须把 add_rows 的返回
（0=被丢弃）传回给 RecorderUI，使完成提示与实际插入结果一致。
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.ui.controllers import profile_controller as pc_mod  # noqa: E402
from fukua_rpa.ui.controllers import recording_controller as rc_mod  # noqa: E402


class _FakeHost:
    """Weak-referenceable fake host（SimpleNamespace 不支持弱引用）。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ClearEditorHistoryTests(unittest.TestCase):
    """FR-901-001: _clear_editor_history 清空 undo/redo 栈。"""

    def test_clears_undo_and_redo_stacks(self):
        host = _FakeHost(
            workflow_editor=_FakeHost(
                undo_stack=[1, 2, 3],
                redo_stack=[4, 5],
                _last_undo_hash="old-baseline",
            )
        )
        pc_mod.ProfileController(host)._clear_editor_history()
        self.assertEqual(host.workflow_editor.undo_stack, [])
        self.assertEqual(host.workflow_editor.redo_stack, [])
        # FR-901-001 返修：去重基线同步重置（workflow_undo.py:83/89）
        self.assertIsNone(host.workflow_editor._last_undo_hash)

    def test_no_workflow_editor_is_safe(self):
        host = _FakeHost()
        pc_mod.ProfileController(host)._clear_editor_history()  # 不应抛异常


class OnProfileChangedClearsUndoHashTests(unittest.TestCase):
    """FR-UICORE-001: 方案切换路径须重置去重基线 _last_undo_hash。"""

    def test_switch_resets_undo_hash(self):
        editor = _FakeHost(undo_stack=[1], redo_stack=[2], _last_undo_hash="stale")
        host = _FakeHost(
            is_switching_profile=False,
            current_profile_name="A",
            profiles_data={"A": {}, "B": {}},
            get_current_ui_config=lambda: {},
            profile_model=_FakeHost(select=lambda n: {"name": n}),
            apply_ui_config=lambda cfg: True,
            workflow_editor=editor,
            mark_dirty=lambda **k: None,
            append_application_log=lambda *a, **k: None,
        )
        pc_mod.ProfileController(host).on_profile_changed("B")
        self.assertEqual(editor.undo_stack, [])
        self.assertEqual(editor.redo_stack, [])
        # FR-UICORE-001: 切换路径必须重置去重基线，否则新方案首快照哈希
        # 恰等于旧基线时首编辑被抑制不进撤销栈。
        self.assertIsNone(editor._last_undo_hash)


class RecordingAddTasksTests(unittest.TestCase):
    """FR-901-002: _add_tasks 必须把 add_rows 的插入结果传回。"""

    def test_add_tasks_returns_add_rows_result(self):
        host = _FakeHost(add_rows=lambda tasks: 0)  # 运行期编辑锁：返回 0
        controller = rc_mod.RecordingController(host)
        self.assertEqual(controller._add_tasks([{"type": 1.0}]), 0)

    def test_add_tasks_forwards_success(self):
        host = _FakeHost(add_rows=lambda tasks: None)  # 成功路径返回 None
        controller = rc_mod.RecordingController(host)
        self.assertIsNone(controller._add_tasks([{"type": 1.0}]))


class DeleteProfileApplyFailureTests(unittest.TestCase):
    """FR-004: 删除方案后 apply 失败不得标脏，避免自动保存跨方案覆盖。

    根因：delete_current_profile 忽略 apply_ui_config 的返回值并强制
    mark_dirty(force=True)。apply 失败时界面内容仍是"被删方案"，3 秒自动保存
    会把这段内容写入存活方案名（persist_profiles_state 把当前 UI 写入
    profiles_data[current_profile_name]），用户方案被静默改写。
    """

    def _make_host(self, apply_ok):
        return _FakeHost(
            is_switching_profile=False,
            current_profile_name="B",  # 删除 A 后模型已切到 B
            profiles_data={"A": {"name": "A"}, "B": {"name": "B"}},
            profile_model=_FakeHost(
                profile_id_of=lambda n: f"id-{n}",
                delete_current=lambda: None,
            ),
            profile_combo=_FakeHost(
                findText=lambda t: 0,
                removeItem=lambda i: None,
                setCurrentText=lambda t: None,
            ),
            apply_ui_config=lambda cfg: apply_ok,
            workflow_editor=_FakeHost(undo_stack=[], redo_stack=[], _last_undo_hash=None),
            append_application_log=lambda *a, **k: None,
            profile_hotkey_controller=_FakeHost(on_profiles_changed=lambda: None),
        )

    def test_apply_failure_does_not_mark_dirty(self):
        host = self._make_host(apply_ok=False)
        controller = pc_mod.ProfileController(host)

        with (
            mock.patch.object(pc_mod.QMessageBox, "question", return_value=pc_mod.QMessageBox.Yes),
            mock.patch.object(pc_mod.QMessageBox, "warning") as warn,
            mock.patch.object(controller, "mark_dirty") as mark_dirty,
        ):
            controller.delete_current_profile()

        mark_dirty.assert_not_called()
        warn.assert_called_once()

    def test_apply_success_marks_dirty(self):
        """对照组：apply 成功时仍须标脏，保证删除操作正常落盘。"""

        host = self._make_host(apply_ok=True)
        controller = pc_mod.ProfileController(host)

        with (
            mock.patch.object(pc_mod.QMessageBox, "question", return_value=pc_mod.QMessageBox.Yes),
            mock.patch.object(controller, "mark_dirty") as mark_dirty,
        ):
            controller.delete_current_profile()

        mark_dirty.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
