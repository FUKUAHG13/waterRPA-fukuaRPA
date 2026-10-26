import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fukua_rpa.ui.usage_notices_dialog import (
    FirstRunChoiceDialog,
    UsageNoticesDialog,
    show_first_run_choice,
    sorted_usage_categories,
    sorted_usage_notices,
    usage_name_sort_key,
)
from fukua_rpa.usage_notices import (
    FIRST_RUN_CHOICE_DONE_KEY,
    FIRST_USE_NOTICES_ENABLED_KEY,
    NOTICE_BY_KEY,
    USAGE_NOTICES,
    UsageNotice,
    automatic_first_use_notice_pending,
    first_use_notices_enabled,
    set_first_use_notices_enabled,
)
from tests.qt_test_support import QtCallbackGuardMixin


class _SettingsStub:
    @staticmethod
    def value(_key, default=None, **_kwargs):
        return default


class _RecordingSettings:
    """Minimal QSettings stand-in used by first-use gate and choice tests."""

    def __init__(self, **initial):
        self._values = dict(initial)
        self.writes = []

    def value(self, key, default=None, **_kwargs):
        return self._values.get(key, default)

    def setValue(self, key, value):  # noqa: N802 - 匹配 Qt 签名
        self._values[key] = value
        self.writes.append((key, value))

    def sync(self):
        pass


class UsageNoticeCatalogTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_notice_keys_are_unique_and_catalog_is_complete(self):
        keys = [notice.key for notice in USAGE_NOTICES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(NOTICE_BY_KEY))

    def test_editor_and_execution_boundaries_are_explained(self):
        expected = {
            "editor_organization": ("隐藏", "仍会照常执行"),
            "disabled_hidden_entry": ("仅被跳转", "不会产生成功或失败"),
            "explicit_step_call": ("返回", "分支"),
            "batch_step_settings": ("兼容参数", "保持原样"),
            "step_region_precedence": ("替代", "全局"),
            "auxiliary_clicks": ("不参与", "停止脚本"),
            "auto_clicker_middle_button": ("中键", "停止快捷键"),
            "coordinate_preview_dragging": ("隐式顶层组", "不能拖动"),
        }
        for key, phrases in expected.items():
            notice = NOTICE_BY_KEY[key]
            joined = notice.title + notice.body
            for phrase in phrases:
                self.assertIn(phrase, joined)

    def test_three_level_catalog_is_sorted_by_full_pinyin_initials(self):
        categories = sorted_usage_categories()
        self.assertEqual(
            categories,
            ["安全与执行", "操作与兼容", "速度与识图", "数据与隐私"],
        )
        all_safety = [n for n in USAGE_NOTICES if n.category == "安全与执行"]
        safety = sorted_usage_notices("安全与执行")
        safety_titles = [notice.title for notice in safety]
        # Completeness: every "安全与执行" notice appears exactly once. Deriving
        # the expectation from the catalog avoids a brittle hard-coded count that
        # drifts (and silently under-asserts) when a new safety notice is added.
        self.assertEqual(len(safety_titles), len(all_safety))
        self.assertEqual(set(safety_titles), {n.title for n in all_safety})
        # The dialog must return the category's notices sorted by pinyin initials
        # (the same key it documents), not insertion order.
        self.assertEqual(
            safety_titles,
            sorted(safety_titles, key=usage_name_sort_key),
        )
        # Spot-check a few well-known safety notices are present.
        for expected in (
            "首次运行：三种急停方式",
            "运行前确认急停方式",
            "运行导入方案前先检查动作",
        ):
            self.assertIn(expected, safety_titles)
        unknown = (
            UsageNotice("unknown_a", "测试", "未知条目", "A"),
            UsageNotice("unknown_b", "测试", "陌生条目", "B"),
        )
        self.assertEqual(
            [notice.key for notice in sorted_usage_notices("测试", unknown)],
            ["unknown_b", "unknown_a"],
        )
        self.assertEqual(usage_name_sort_key("未知条目"), "WZTM")

        dialog = UsageNoticesDialog(None, _SettingsStub())
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.category_list.count(), len(categories))
        self.assertGreater(dialog.notice_list.count(), 0)
        self.assertTrue(dialog.details.toPlainText().strip())


class FirstUseNoticeSwitchTests(QtCallbackGuardMixin, unittest.TestCase):
    """反馈项：首用提示总开关只控制自动弹出，且不污染已看过状态。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_is_enabled_and_gate_reports_pending(self):
        settings = _RecordingSettings()
        self.assertTrue(first_use_notices_enabled(settings))
        self.assertTrue(
            automatic_first_use_notice_pending(
                settings, "onboarding/script_emergency_stop_methods_v1_seen"
            )
        )

    def test_disabled_switch_suppresses_without_marking_seen(self):
        settings = _RecordingSettings()
        set_first_use_notices_enabled(settings, False)
        key = "onboarding/script_emergency_stop_methods_v1_seen"

        self.assertFalse(automatic_first_use_notice_pending(settings, key))
        self.assertNotIn(
            key,
            settings._values,
            "关闭总开关不得把 *_seen 标记为已看过",
        )
        self.assertEqual(
            [k for k, _ in settings.writes], [FIRST_USE_NOTICES_ENABLED_KEY]
        )

    def test_seen_notice_stays_suppressed_after_reenabling(self):
        settings = _RecordingSettings(
            **{"onboarding/script_emergency_stop_methods_v1_seen": True}
        )
        set_first_use_notices_enabled(settings, False)
        set_first_use_notices_enabled(settings, True)
        self.assertFalse(
            automatic_first_use_notice_pending(
                settings, "onboarding/script_emergency_stop_methods_v1_seen"
            ),
            "真正看过的提示在重新开启后不应重复弹出",
        )

    def test_usage_dialog_distinguishes_seen_and_switched_off(self):
        settings = _RecordingSettings(
            **{"onboarding/script_emergency_stop_methods_v1_seen": True}
        )
        dialog = UsageNoticesDialog(None, settings)
        self.addCleanup(dialog.deleteLater)
        set_first_use_notices_enabled(settings, False)
        dialog._show_notice(
            _Item("script_emergency_stop_methods"), None
        )
        text = dialog.details.toPlainText()
        self.assertIn("自动提示已关闭", text)
        self.assertNotIn("当前已经提示过。", text)

        set_first_use_notices_enabled(settings, True)
        dialog._show_notice(
            _Item("script_emergency_stop_methods"), None
        )
        self.assertIn("当前已经提示过。", dialog.details.toPlainText())


class _Item:
    """QListWidgetItem stand-in exposing the data used by ``_show_notice``."""

    def __init__(self, key):
        self._key = key

    def data(self, _role):
        return self._key


class FirstRunChoiceTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, settings, outcome, parent=None):
        from unittest import mock

        dialog = FirstRunChoiceDialog(None)
        # FR-RISK-20261010-3103：无父对话框统一走既有清理惯例（与本文件
        # FirstUseNoticeSwitchTests 的 addCleanup 模式一致）。
        self.addCleanup(dialog.deleteLater)
        patched_class = mock.MagicMock(return_value=dialog)
        patched_class.RESULT_CLOSE_NOTICES = (
            FirstRunChoiceDialog.RESULT_CLOSE_NOTICES
        )
        patched_class.RESULT_KEEP_NOTICES = FirstRunChoiceDialog.RESULT_KEEP_NOTICES
        with mock.patch.object(
            dialog, "exec", return_value=outcome
        ), mock.patch(
            "fukua_rpa.ui.usage_notices_dialog.FirstRunChoiceDialog",
            patched_class,
        ):
            return show_first_run_choice(parent, settings)

    @staticmethod
    def _host_with_built_checkboxes():
        """主窗口在构造时就按默认值勾选的设置控件（复现反馈场景）。"""

        from PySide6.QtWidgets import QCheckBox

        host = type("HostStub", (), {})()
        host.risk_tip_chk = QCheckBox("运行前风险提示")
        host.risk_tip_chk.setChecked(True)
        host.first_use_notices_chk = QCheckBox("首次使用提示")
        host.first_use_notices_chk.setChecked(True)
        return host

    def test_close_notices_turns_off_both_switches(self):
        settings = _RecordingSettings(**{"risk_tip_enabled": True})
        turned_off = self._run(
            settings, FirstRunChoiceDialog.RESULT_CLOSE_NOTICES
        )
        self.assertTrue(turned_off)
        self.assertFalse(first_use_notices_enabled(settings))
        self.assertFalse(bool(settings.value("risk_tip_enabled", True, type=bool)))
        self.assertTrue(bool(settings.value(FIRST_RUN_CHOICE_DONE_KEY, False)))
        self.assertEqual(
            [k for k, _ in settings.writes if k == "risk_tip_enabled"], ["risk_tip_enabled"]
        )

    def test_close_choice_syncs_already_built_checkboxes(self):
        """反馈 20261010-1：选“关闭提示”必须同步已构建的设置控件。

        控件在主窗口构造时按旧值勾选；若弹窗只写 QSettings 不同步控件，
        设置页显示就与持久状态不一致（用户看到的仍是旧开关状态）。"""
        settings = _RecordingSettings(**{"risk_tip_enabled": True})
        host = self._host_with_built_checkboxes()

        turned_off = self._run(
            settings, FirstRunChoiceDialog.RESULT_CLOSE_NOTICES, parent=host
        )

        self.assertTrue(turned_off)
        self.assertFalse(
            host.risk_tip_chk.isChecked(),
            "运行前风险提示复选框必须与持久化的关闭状态一致",
        )
        self.assertFalse(
            host.first_use_notices_chk.isChecked(),
            "首次使用提示复选框必须与持久化的关闭状态一致",
        )
        # 控件与持久值一致后，后续任何基于控件的回写都以正确状态为起点。
        self.assertFalse(host.risk_tip_chk.isChecked())

    def test_keep_and_close_window_leave_checkboxes_checked(self):
        for outcome in (
            FirstRunChoiceDialog.RESULT_KEEP_NOTICES,
            0,
        ):
            settings = _RecordingSettings()
            host = self._host_with_built_checkboxes()
            self._run(settings, outcome, parent=host)
            self.assertTrue(
                host.risk_tip_chk.isChecked(), f"outcome={outcome} 应保持勾选"
            )
            self.assertTrue(host.first_use_notices_chk.isChecked())

    def test_keep_notices_leaves_risk_switch_untouched(self):
        settings = _RecordingSettings()
        turned_off = self._run(
            settings, FirstRunChoiceDialog.RESULT_KEEP_NOTICES
        )
        self.assertFalse(turned_off)
        self.assertTrue(first_use_notices_enabled(settings))
        self.assertNotIn("risk_tip_enabled", settings._values)
        self.assertTrue(bool(settings.value(FIRST_RUN_CHOICE_DONE_KEY, False)))

    def test_closing_the_dialog_keeps_notices_and_records_choice(self):
        settings = _RecordingSettings()
        # reject() 对应直接关闭窗口：保留提示并记录选择已结束。
        turned_off = self._run(settings, 0)
        self.assertFalse(turned_off)
        self.assertTrue(first_use_notices_enabled(settings))
        self.assertNotIn("risk_tip_enabled", settings._values)
        self.assertTrue(bool(settings.value(FIRST_RUN_CHOICE_DONE_KEY, False)))


if __name__ == "__main__":
    unittest.main()
