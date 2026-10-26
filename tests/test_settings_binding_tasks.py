"""FR-20260811-D12-001: apply_global_settings 缺 tasks 键时不得清空步骤。

含 D11/D12 建议的守卫：保存缩放经 apply_global_settings 恢复时 tasks 不被破坏。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fukua_rpa.ui.settings_binding import apply_global_settings


class FakeHost:
    """最小 host 桩：capture 与 apply 用到的属性由 mock 提供足够桩。"""

    def __init__(self):
        self._last_config_apply_error = ""

    class _Edit:
        def __init__(self, value=""):
            self._value = value

        def text(self):
            return self._value

        def setText(self, value):
            self._value = value

        def currentText(self):
            return self._value

        def currentIndex(self):
            return 0

        def findData(self, _value):
            return 0

        def setCurrentIndex(self, _index):
            pass

        def setChecked(self, _value):
            pass

        def isChecked(self):
            return False

    def __getattr__(self, name):
        return self._Edit()


class ApplyGlobalSettingsTasksGuardTests(unittest.TestCase):
    def setUp(self):
        # 固定 previous：当前界面已有一组步骤。
        self.previous = {"tasks": [{"type": 5.0, "value": "step-1"}]}
        self.host = FakeHost()
        self.capture_patch = mock.patch(
            "fukua_rpa.ui.settings_binding.capture_global_settings",
            return_value=self.previous,
        )
        self.capture_patch.start()
        self.addCleanup(self.capture_patch.stop)

    def _apply(self, cfg):
        with mock.patch(
            "fukua_rpa.ui.settings_binding._apply_global_settings_unchecked"
        ) as apply_unchecked:
            ok = apply_global_settings(self.host, cfg)
        return ok, apply_unchecked

    def test_cfg_without_tasks_preserves_current_step_list(self):
        """传入缺 tasks 键的 cfg 时，注入当前界面 tasks，不得按空列表清空步骤。"""
        cfg = {
            "sound_alert_audio_path": "",
            "theme_mode": "light",
            "ui_scale": "100",
            # 刻意缺 "tasks" 键（调用方误传部分配置）。
        }
        ok, apply_unchecked = self._apply(cfg)
        self.assertTrue(ok)
        applied = apply_unchecked.call_args[0][1]
        self.assertEqual(applied["tasks"], [{"type": 5.0, "value": "step-1"}])

    def test_cfg_with_tasks_applies_them_normally(self):
        """正常传入含 tasks 的完整 cfg 时，直接应用传入的 tasks。"""
        cfg = {
            "theme_mode": "dark",
            "ui_scale": "100",
            "tasks": [{"type": 7.0, "value": "new-step"}],
        }
        ok, apply_unchecked = self._apply(cfg)
        self.assertTrue(ok)
        applied = apply_unchecked.call_args[0][1]
        # validate_profile_for_ui 会规范化步骤（补 step_id 等默认字段），
        # 断言关键字段而非整 dict 相等。
        self.assertEqual(len(applied["tasks"]), 1)
        self.assertEqual(applied["tasks"][0]["value"], "new-step")
        self.assertEqual(applied["tasks"][0]["type"], 7.0)

    def test_int_field_tolerates_float_strings(self):
        """FR-20260812-D12-a: 数值键字段级容错——"3.0"/"3e0" 可解析，
        inf/非数字回退默认值，不得使整份配置应用回滚。"""
        from fukua_rpa.ui.settings_binding import _int_field

        self.assertEqual(_int_field("3.0", 3), 3)
        self.assertEqual(_int_field("3e0", 64), 3)
        self.assertEqual(_int_field(5, 3), 5)
        self.assertEqual(_int_field("inf", 3), 3)
        self.assertEqual(_int_field("1e309", 3), 3)
        self.assertEqual(_int_field("abc", 3), 3)
        self.assertEqual(_int_field("", 3), 3)
        self.assertEqual(_int_field(None, 3), 3)
        self.assertEqual(_int_field("3.5", 3), 3)  # 非整数回退


class RollbackFallbackSnapshotTests(unittest.TestCase):
    """ADV-SCOPE008-001：捕获失败时的兜底快照不得清空步骤列表。

    可达路径（05 §4.7 的「两次失败同时发生」）：``capture_global_settings`` 抛错
    → 兜底 ``default_profile_config()``（``tasks == []``）→ 前向应用也抛错 → 回滚
    用空步骤列表重建编辑器 → 主窗口 3 s 防抖自动保存把空内容写盘（数据丢失）。
    """

    UI_TASKS = [{"type": 5.0, "value": "step-1"}, {"type": 1.0, "value": "step-2"}]

    def setUp(self):
        self.host = FakeHost()
        self.host.snapshot_tasks = lambda: [dict(t) for t in self.UI_TASKS]

    def _run(self, cfg, *, capture_fails, apply_fails):
        """返回 (ok, 载荷序列)。载荷序列为 _apply_global_settings_unchecked 收到的 config。"""
        payloads = []

        def fake_apply(_host, config, **_kwargs):
            payloads.append(dict(config))
            if apply_fails and len(payloads) == 1:
                raise RuntimeError("apply boom")
            return None

        patches = [
            mock.patch(
                "fukua_rpa.ui.settings_binding._apply_global_settings_unchecked",
                side_effect=fake_apply,
            )
        ]
        if capture_fails:
            patches.append(
                mock.patch(
                    "fukua_rpa.ui.settings_binding.capture_global_settings",
                    side_effect=OSError("capture boom"),
                )
            )
        else:
            patches.append(
                mock.patch(
                    "fukua_rpa.ui.settings_binding.capture_global_settings",
                    return_value={"tasks": [dict(t) for t in self.UI_TASKS]},
                )
            )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return apply_global_settings(self.host, cfg), payloads

    def test_rollback_after_capture_failure_keeps_current_steps(self):
        cfg = {"theme_mode": "dark", "ui_scale": "100", "tasks": list(self.UI_TASKS)}
        ok, payloads = self._run(cfg, capture_fails=True, apply_fails=True)
        self.assertFalse(ok)
        self.assertEqual(len(payloads), 2, "应有前向 + 回滚两次应用")
        self.assertEqual(payloads[1]["tasks"], self.UI_TASKS)

    def test_missing_tasks_with_capture_failure_keeps_current_steps(self):
        """cfg 缺 tasks 键时，前向注入也不得用兜底的空列表。"""
        cfg = {"theme_mode": "dark", "ui_scale": "100"}
        ok, payloads = self._run(cfg, capture_fails=True, apply_fails=True)
        self.assertFalse(ok)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["tasks"], self.UI_TASKS)
        self.assertEqual(payloads[1]["tasks"], self.UI_TASKS)

    def test_capture_ok_still_uses_the_real_snapshot(self):
        """不变量：capture 正常时回滚仍使用真实快照。"""
        cfg = {"theme_mode": "dark", "ui_scale": "100", "tasks": list(self.UI_TASKS)}
        ok, payloads = self._run(cfg, capture_fails=False, apply_fails=True)
        self.assertFalse(ok)
        self.assertEqual(payloads[1]["tasks"], self.UI_TASKS)

    def test_fallback_snapshot_never_raises_even_if_tasks_unreadable(self):
        """SAFETY：兜底快照自身抛错会让回滚彻底失败，比字段不全更糟。

        ``snapshot_tasks()`` 不可读时如实记录警告并保留空列表——这是已知的残余
        边界，用例把它钉住以免被误当作「已完全消除数据丢失风险」。
        """

        def boom():
            raise RuntimeError("snapshot boom")

        self.host.snapshot_tasks = boom
        cfg = {"theme_mode": "dark", "ui_scale": "100", "tasks": list(self.UI_TASKS)}
        with self.assertLogs("fukua_rpa.ui.settings_binding", level="WARNING"):
            ok, payloads = self._run(cfg, capture_fails=True, apply_fails=True)
        self.assertFalse(ok)
        self.assertEqual(len(payloads), 2, "回滚必须仍然发生")
        self.assertEqual(payloads[1]["tasks"], [])


if __name__ == "__main__":
    unittest.main()
