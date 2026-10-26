"""Guards for the REVIEW-20261006-001 batch 7 code changes.

Two items in that batch changed behaviour rather than only documentation:

* ``DEC-20260930-002-04`` — the coordinate-step direction used to be matched by
  **prefix** (``direction.startswith("鑷")`` / ``startswith("绉诲姩")``), so any
  unknown value beginning with those characters was silently accepted as a legal
  direction. The maintainer's ruling keeps only a fixed table of verified aliases,
  requires unknown values to be preserved verbatim, and forbids prefix guessing.
* ``DOC-20261006-009`` — ``diagnostics`` hardcoded ``UIAutomationClient_VC140_X64.dll``
  while both PyInstaller specs record the decision to discover the name instead. An
  upstream label change would make the frozen-package self-check report a false
  ``missing`` and block ``verify_release``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MojibakeDirectionAliasTests(unittest.TestCase):
    """DEC-20260930-002-04: fixed aliases only, no prefix guessing."""

    def test_every_alias_maps_to_a_standard_direction(self):
        from fukua_rpa.task_model import (
            _DIRECTION_ALIASES,
            COORD_STEP_DIRECTIONS,
            normalize_coord_step_direction,
        )

        self.assertTrue(_DIRECTION_ALIASES, "别名表不得为空")
        for alias, standard in _DIRECTION_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(standard, COORD_STEP_DIRECTIONS)
                self.assertEqual(normalize_coord_step_direction(alias), standard)

    def test_aliases_are_not_themselves_standard_names(self):
        """An alias that equals a standard name would make the table pointless."""
        from fukua_rpa.task_model import _DIRECTION_ALIASES, COORD_STEP_DIRECTIONS

        for alias in _DIRECTION_ALIASES:
            self.assertNotIn(alias, COORD_STEP_DIRECTIONS)

    def test_standard_names_pass_through_unchanged(self):
        from fukua_rpa.task_model import (
            COORD_STEP_DIRECTIONS,
            normalize_coord_step_direction,
        )

        for standard in COORD_STEP_DIRECTIONS:
            with self.subTest(standard=standard):
                self.assertEqual(normalize_coord_step_direction(standard), standard)

    def test_unknown_values_are_returned_verbatim(self):
        """Preserved, never rewritten and never coerced to a legal direction."""
        from fukua_rpa.task_model import normalize_coord_step_direction

        for unknown in (
            "向上啊",
            "鑷定义偏移的东西",
            "绉诲姩到某个地方",
            "",
            "nonsense",
        ):
            with self.subTest(unknown=unknown):
                self.assertEqual(normalize_coord_step_direction(unknown), unknown)

    def test_unknown_values_are_not_known_directions(self):
        from fukua_rpa.task_model import is_known_coord_step_direction

        for unknown in ("向上啊", "鑷X", "绉诲姩YY", ""):
            with self.subTest(unknown=unknown):
                self.assertFalse(is_known_coord_step_direction(unknown))

    def test_former_prefix_guesses_are_no_longer_accepted(self):
        """Strings the old ``startswith`` check would have matched must now fail.

        The samples are derived from the alias table's own keys rather than typed
        by hand: the old check matched a *prefix*, so the exact leading characters
        are what matter, and hand-typed CJK is not reliably byte-identical across
        editors and shells.
        """
        from fukua_rpa.task_model import (
            _DIRECTION_ALIASES,
            coord_step_delta_values,
            is_known_coord_step_direction,
        )

        prefixes = set()
        for alias in _DIRECTION_ALIASES:
            prefixes.add(alias[0])
            prefixes.add(alias[:2])
        self.assertTrue(prefixes, "无法从别名表推导前缀样本")
        for guessed in sorted(prefixes):
            if guessed in _DIRECTION_ALIASES:
                continue  # a real alias is supposed to be accepted
            with self.subTest(guessed=guessed):
                self.assertFalse(is_known_coord_step_direction(guessed))
                self.assertEqual(
                    coord_step_delta_values(guessed, 10.0, 3.0, 4.0),
                    (0.0, 0.0),
                    "前缀猜测必须已被移除",
                )

    def test_standard_directions_still_produce_the_expected_offsets(self):
        from fukua_rpa.task_model import coord_step_delta_values

        self.assertEqual(coord_step_delta_values("向上", 10.0, 3.0, 4.0), (0.0, -10.0))
        self.assertEqual(coord_step_delta_values("向下", 10.0, 3.0, 4.0), (0.0, 10.0))
        self.assertEqual(coord_step_delta_values("向左", 10.0, 3.0, 4.0), (-10.0, 0.0))
        self.assertEqual(coord_step_delta_values("向右", 10.0, 3.0, 4.0), (10.0, 0.0))
        self.assertEqual(
            coord_step_delta_values("自定义偏移", 10.0, 3.0, 4.0), (3.0, 4.0)
        )

    def test_alias_yields_the_same_offset_as_its_standard_name(self):
        from fukua_rpa.task_model import (
            _DIRECTION_ALIASES,
            coord_step_delta_values,
        )

        for alias, standard in _DIRECTION_ALIASES.items():
            if standard == "移动到新点位":
                continue  # consumed by build_coord_step_positions, not delta_values
            with self.subTest(alias=alias):
                self.assertEqual(
                    coord_step_delta_values(alias, 10.0, 3.0, 4.0),
                    coord_step_delta_values(standard, 10.0, 3.0, 4.0),
                )

    def test_move_to_point_alias_still_builds_the_path(self):
        """The alias must work end-to-end, not just in the delta helper."""
        from fukua_rpa.task_model import _DIRECTION_ALIASES, build_coord_step_positions

        alias = next(
            key
            for key, value in _DIRECTION_ALIASES.items()
            if value == "移动到新点位"
        )
        options = {
            "direction": alias,
            "point": "100,100",
            "max_steps": 3,
            "max_distance": 0,
        }
        self.assertEqual(len(build_coord_step_positions(0, 0, options)), 3)

    @staticmethod
    def _coord_step_task(direction: str) -> dict:
        """A coordinate-click step with coordinate-stepping enabled.

        ``_validate_coordinate_step`` only runs when ``coord_step_en`` is set and
        the step is a coordinate click, so both conditions are required here.
        """
        return {
            "type": 1.0,
            "step_id": "s1",
            "value": "10,10",
            "coord_step_en": True,
            "coord_step_every": "1",
            "coord_step_direction": direction,
            "coord_step_distance": "10",
            "coord_step_dx": "0",
            "coord_step_dy": "0",
            "coord_step_point": "",
            "coord_step_max_steps": "0",
            "coord_step_max_distance": "0",
            "coord_step_reset_after": "0",
        }

    def test_known_alias_passes_pre_run_validation(self):
        """The migration entry point must keep working, not just the delta helper."""
        from fukua_rpa.task_model import _DIRECTION_ALIASES
        from fukua_rpa.validation import validate_task_list_all

        alias = next(
            key for key, value in _DIRECTION_ALIASES.items() if value == "向上"
        )
        errors = validate_task_list_all([self._coord_step_task(alias)], {})
        self.assertEqual(errors, [], f"已知别名必须通过校验，实际：{errors}")

    def test_standard_direction_passes_pre_run_validation(self):
        from fukua_rpa.validation import validate_task_list_all

        errors = validate_task_list_all([self._coord_step_task("向上")], {})
        self.assertEqual(errors, [], f"标准方向必须通过校验，实际：{errors}")

    def test_validation_rejects_an_unknown_direction_with_its_own_text(self):
        """The decision's core requirement: unknown values must not run."""
        from fukua_rpa.validation import validate_task_list_all

        unknown = "not-a-real-direction"
        errors = validate_task_list_all([self._coord_step_task(unknown)], {})
        self.assertTrue(errors, "未知方向必须在运行前被拒绝")
        self.assertIn(
            unknown,
            errors[0],
            "错误信息必须保留用户原始值，不得替换为猜测出的方向",
        )


class UiaHelperDiscoveryTests(unittest.TestCase):
    """DOC-20261006-009: no hardcoded VC runtime label in diagnostics."""

    def _bin_dir(self, *names: str) -> str:
        import tempfile

        path = tempfile.mkdtemp(prefix="uia-helper-")
        self.addCleanup(__import__("shutil").rmtree, path, True)
        for name in names:
            (Path(path) / name).write_bytes(b"")
        return path

    def test_known_labels_are_accepted(self):
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        for name in (
            "UIAutomationClient_VC140_X64.dll",
            "UIAutomationClient_VC142_X64.dll",
            "UIAutomationClient_X64.dll",
        ):
            with self.subTest(name=name):
                self.assertTrue(_uia_x64_helper_present(self._bin_dir(name)))

    def test_matching_is_case_insensitive(self):
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        self.assertTrue(
            _uia_x64_helper_present(self._bin_dir("uiautomationclient_vc140_x64.dll"))
        )

    def test_non_x64_variants_are_rejected(self):
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        for name in (
            "UIAutomationClient_VC140_X86.dll",
            "UIAutomationClient_VC140_ARM64.dll",
            "unrelated.dll",
        ):
            with self.subTest(name=name):
                self.assertFalse(_uia_x64_helper_present(self._bin_dir(name)))

    def test_upstream_label_change_does_not_break_the_check(self):
        """The regression that motivated DOC-009: a new VC label must still pass."""
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        self.assertTrue(
            _uia_x64_helper_present(self._bin_dir("UIAutomationClient_VC999_X64.dll"))
        )

    def test_missing_directory_is_not_an_error(self):
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        self.assertFalse(_uia_x64_helper_present("no/such/dir"))

    def test_empty_directory_is_missing(self):
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        self.assertFalse(_uia_x64_helper_present(self._bin_dir()))

    def test_source_no_longer_hardcodes_a_vc_label(self):
        """Static guard against a concrete VC-labelled filename coming back.

        A docstring may legitimately name the ``UIAutomationClient`` prefix when it
        explains the matching rule, so only ``*.dll`` literals count — that is what
        the defect was (a hardcoded filename handed to a filesystem call).
        """
        import ast

        source = (ROOT / "fukua_rpa" / "diagnostics.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if (
                    "UIAutomationClient" in value
                    and value.endswith(".dll")
                    and value not in docstrings
                ):
                    offenders.add(value)
        self.assertEqual(
            offenders, set(), f"不得再硬编码具体 VC runtime 文件名：{sorted(offenders)}"
        )

    def test_spec_decision_is_honoured_by_the_same_matching_rule(self):
        """Both specs discover names; diagnostics must accept a superset."""
        from fukua_rpa.diagnostics import _uia_x64_helper_present

        self.assertTrue(
            _uia_x64_helper_present(
                self._bin_dir("UIAutomationClient_VC140_ARM64_X64.dll")
            ),
            "任何带 X64 后缀的变体都应被接受",
        )


if __name__ == "__main__":
    unittest.main()
