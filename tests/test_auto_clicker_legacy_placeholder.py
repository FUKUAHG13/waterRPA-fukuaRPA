"""FR-20261006-004 / DEC-20260930-002-03 (A1): the 28.0 auto-clicker step.

Maintainer ruling A1 chose "read-only placeholder":

* a legacy profile's 28.0 step keeps its **type and every original parameter**;
* the step-type combo shows the name as a **disabled placeholder** rather than
  silently snapping to the first item;
* a **newly created** row must not offer the option at all;
* no permanent cross-version compatibility promise is added.

Verification is on ``collect_task_row_data`` round-trips, not on combo text —
A1 explicitly rules out "只验证下拉框文字".
"""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from fukua_rpa.commands import COMMAND_BY_CODE, command_names
from fukua_rpa.constants import TASK_TYPE_AUTO_CLICKER
from fukua_rpa.ui.task_row import TaskRow
from fukua_rpa.ui.task_row_binding import apply_task_row_data, collect_task_row_data

_APP = QApplication.instance() or QApplication([])


def _dispose(row: TaskRow) -> None:
    row.deleteLater()
    _APP.processEvents()


class AutoClickerPlaceholderTests(unittest.TestCase):
    """28.0 loads as a disabled placeholder; new rows never offer it."""

    def _legacy_payload(self) -> dict:
        """A 28.0 step with the parameters A1 requires to survive verbatim."""
        return {
            "type": TASK_TYPE_AUTO_CLICKER,
            "value": "",
            "ac_button": "left",
            "ac_click_count": 25,
            "custom_en": True,
            "point_limit_en": True,
        }

    def test_command_names_excludes_auto_clicker(self) -> None:
        """Baseline: the option is absent from every selector we build."""
        for include_advanced in (True, False):
            with self.subTest(include_advanced=include_advanced):
                self.assertNotIn(
                    COMMAND_BY_CODE[TASK_TYPE_AUTO_CLICKER].name,
                    command_names(include_advanced=include_advanced),
                )
                # preserve_names must NOT be able to smuggle a tool-only action in:
                # that is exactly why the placeholder path exists.
                self.assertNotIn(
                    COMMAND_BY_CODE[TASK_TYPE_AUTO_CLICKER].name,
                    command_names(
                        include_advanced=include_advanced,
                        preserve_names=(COMMAND_BY_CODE[TASK_TYPE_AUTO_CLICKER].name,),
                    ),
                )

    def test_legacy_step_round_trips_type_and_parameters(self) -> None:
        """Loading then collecting must not rewrite 28.0 or drop its fields."""
        row = TaskRow(lambda _row: None)
        self.addCleanup(_dispose, row)
        source = self._legacy_payload()

        apply_task_row_data(row, dict(source))
        collected = collect_task_row_data(row)

        self.assertEqual(
            collected.get("type"),
            TASK_TYPE_AUTO_CLICKER,
            "28.0 不得被改写成下拉第一项",
        )
        for key in ("ac_button", "ac_click_count", "custom_en", "point_limit_en"):
            with self.subTest(field=key):
                self.assertEqual(
                    collected.get(key),
                    source[key],
                    f"{key} 必须在往返后逐项保持原样",
                )

    def test_placeholder_is_present_but_not_selectable(self) -> None:
        """The legacy name is visible as a disabled item, so text matches disk."""
        row = TaskRow(lambda _row: None)
        self.addCleanup(_dispose, row)
        name = COMMAND_BY_CODE[TASK_TYPE_AUTO_CLICKER].name

        apply_task_row_data(row, self._legacy_payload())

        combo = row.type_combo
        index = combo.findText(name)
        self.assertGreaterEqual(
            index, 0, "旧方案的 28.0 必须在下拉中可见，否则界面与磁盘值不一致"
        )
        self.assertFalse(
            combo.model().item(index).isEnabled(),
            "占位项必须是禁用的，用户不能把连点器选成新的步骤类型",
        )
        self.assertEqual(combo.currentText(), name)

    def test_new_row_offers_no_auto_clicker_option(self) -> None:
        """A1: 新建步骤不得出现该选项。"""
        row = TaskRow(lambda _row: None)
        self.addCleanup(_dispose, row)

        apply_task_row_data(row, {"type": 1.0, "value": ""})

        names = [row.type_combo.itemText(i) for i in range(row.type_combo.count())]
        self.assertNotIn(
            COMMAND_BY_CODE[TASK_TYPE_AUTO_CLICKER].name,
            names,
            "普通步骤的下拉不得包含连点器",
        )

    def test_switching_away_and_back_does_not_lose_the_step(self) -> None:
        """After editing another row's type, the 28.0 row still reports 28.0."""
        auto_row = TaskRow(lambda _row: None)
        self.addCleanup(_dispose, auto_row)
        source = self._legacy_payload()

        apply_task_row_data(auto_row, dict(source))
        # Simulate the user working elsewhere: apply a different type to this row
        # and then restore the saved payload, as a save/reload cycle does.
        apply_task_row_data(auto_row, {"type": 1.0, "value": "10,20"})
        apply_task_row_data(auto_row, dict(source))

        self.assertEqual(collect_task_row_data(auto_row).get("type"), TASK_TYPE_AUTO_CLICKER)


if __name__ == "__main__":  # pragma: no cover - manual probe entry
    unittest.main()
