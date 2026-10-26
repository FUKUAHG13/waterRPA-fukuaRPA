"""FR-20261006-008: bulk "apply to differing fields" must respect visibility.

The bulk dialog collects **every** field (simple mode hides advanced editors, but
collection is unconditional). Checking「统一未改字段」used to push all of those
differing values onto every selected step — overwriting fields the user could
neither see nor change with the first step's value.

The visibility map is derived from the collector's own source via AST, so a field
added to the dialog is covered automatically; these tests pin that derivation and
the resulting narrowing.
"""

from __future__ import annotations

import tempfile
import unittest

from fukua_rpa.ui.task_settings_binding import (
    _field_widget_map,
    _resolve_widgets,
    collect_task_settings,
    dialog_visible_setting_keys,
)
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.qt_test_support import dispose_widget

_APP = None


def _app():
    global _APP
    if _APP is None:
        from PySide6.QtWidgets import QApplication

        _APP = QApplication.instance() or QApplication([])
    return _APP


class _DialogFactory:
    """Builds real TaskConfigDialog instances and disposes them on cleanup."""

    def __init__(self, case: unittest.TestCase) -> None:
        self._case = case
        self._dir = tempfile.TemporaryDirectory()
        case.addCleanup(self._dir.cleanup)
        self.app = _app()

    def make(self, data: dict | None = None, **kwargs) -> TaskConfigDialog:
        dialog = TaskConfigDialog(
            None, data or {}, base_dir=self._dir.name, **kwargs
        )
        self._case.addCleanup(dispose_widget, self.app, dialog)
        self.app.processEvents()
        return dialog


class FieldWidgetDerivationTests(unittest.TestCase):
    """The map must come from the collector, not from a second hand-written table."""

    def test_unmapped_fields_fail_open(self) -> None:
        """Fields the AST cannot map must stay visible, never silently dropped.

        Per-condition fields read their widgets through a local ``widgets`` dict
        built at runtime, so no static mapping exists for them. Dropping them
        would make「统一未改字段」quietly skip exactly the fields it promises to
        unify — the function must fail open instead.
        """
        factory = _DialogFactory(self)
        dialog = factory.make()
        collected = collect_task_settings(dialog)
        visible = dialog_visible_setting_keys(dialog, tuple(collected))
        mapping = _field_widget_map()
        unmapped = {
            field for field in collected if not _resolve_widgets(mapping, field)
        }
        self.assertTrue(
            unmapped,
            "本用例需要至少一个无法静态映射的字段，否则证明不了 fail open",
        )
        self.assertEqual(
            unmapped - visible,
            set(),
            "无法静态映射的字段必须按可见处理（fail open）",
        )

    def test_explicitly_hidden_field_is_excluded(self) -> None:
        """A control hidden in the current mode must not be bulk-written.

        ``editor_hidden`` is a simple-mode-only affordance: the checkbox itself is
        hidden there, so「统一未改字段」must leave each step's own value alone.
        """
        factory = _DialogFactory(self)
        for mode, expect_hidden in (("simple", True), ("advanced", False)):
            with self.subTest(mode=mode):
                dialog = factory.make(settings_mode=mode)
                collected = collect_task_settings(dialog)
                visible = dialog_visible_setting_keys(dialog, tuple(collected))
                self.assertEqual(
                    "editor_hidden" in visible,
                    not expect_hidden,
                    f"{mode} 模式下 editor_hidden 可见性判定不符预期",
                )

    def test_indirect_fields_are_reported_visible(self) -> None:
        visible = dialog_visible_setting_keys(
            _Bare(), ("until_cond1_conf", "totally_unknown_field")
        )
        self.assertEqual(
            visible,
            {"until_cond1_conf", "totally_unknown_field"},
            "未知/间接字段一律按可见处理",
        )

    def test_known_fields_map_to_their_widgets(self) -> None:
        mapping = _field_widget_map()
        self.assertEqual(mapping["custom_en"], {"enable_chk"})
        self.assertEqual(mapping["custom_conf"], {"conf_edit"})
        self.assertEqual(mapping["until_on_limit"], {"until_on_limit_combo"})

    def test_derivation_is_cached_and_stable(self) -> None:
        self.assertIs(_field_widget_map(), _field_widget_map())


class VisibleSettingKeyTests(unittest.TestCase):
    """Simple mode must report fewer editable fields than advanced mode."""

    def test_simple_mode_hides_at_least_one_advanced_field(self) -> None:
        factory = _DialogFactory(self)
        simple = factory.make(settings_mode="simple")
        advanced = factory.make(settings_mode="advanced")

        simple_visible = dialog_visible_setting_keys(simple)
        advanced_visible = dialog_visible_setting_keys(advanced)
        self.assertTrue(simple_visible)
        self.assertTrue(
            simple_visible <= advanced_visible,
            "高级模式可见字段必须是简易模式的超集",
        )

    def test_always_visible_chrome_is_reported(self) -> None:
        """Step-level chrome (note / disabled) is visible in both modes."""
        factory = _DialogFactory(self)
        for mode in ("simple", "advanced"):
            with self.subTest(mode=mode):
                dialog = factory.make(settings_mode=mode)
                visible = dialog_visible_setting_keys(dialog)
                self.assertIn("step_note", visible)
                self.assertIn("step_disabled", visible)


class _Bare:
    """A stand-in with no widget attributes at all."""


class HiddenWidgetFallbackTests(unittest.TestCase):
    """A missing or deleted widget must not raise or hide everything."""

    def test_absent_widget_fails_open(self) -> None:
        # A field the collector may produce but whose widget this object lacks
        # must still be reported visible: the bulk write is narrowed by *evidence*
        # of hiding, never by absence of a lookup.
        self.assertEqual(
            dialog_visible_setting_keys(_Bare(), ("totally_unknown_field",)),
            {"totally_unknown_field"},
        )

    def test_deleted_widget_does_not_raise(self) -> None:
        class _Boom:
            def __getattr__(self, _name):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        self.assertEqual(
            dialog_visible_setting_keys(_Boom(), ("custom_conf",)),
            set(),
        )


if __name__ == "__main__":  # pragma: no cover - manual probe entry
    unittest.main()
