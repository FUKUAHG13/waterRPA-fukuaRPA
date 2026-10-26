"""Assert every native call site's arity matches its declared argtypes.

FR-20261006-009: the ``ex`` branch passed a preferred-scale *pointer* plus its
length (17 actual arguments) against a 16-slot ``argtypes`` whose 11th slot is a
plain ``c_double``. ctypes raises ``TypeError`` on that mismatch, and the error
escapes the "fall back to OpenCV" contract the caller relies on.

The probe drives the real dispatch with a fake DLL so the argument tuple is
observed directly, rather than trusting a reading of the source.
"""

from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from fukua_rpa.params import MatchOptions
from fukua_rpa.vision import core as core_module


class _FakeFunction:
    """Records call arity; ``argtypes`` is settable like a ctypes foreign func."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] | None = None
        self.restype = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return 0


def _fake_core(has_ex: bool, has_ex2: bool, has_ex3: bool):
    """Build a NativeVisionCore wired to fake DLL exports."""
    core = core_module.NativeVisionCore.__new__(core_module.NativeVisionCore)
    core.last_result_code = 0
    core.load_error = ""
    core.last_failure_category = ""
    core.available = True
    core.has_extended_search = has_ex
    core.has_preferred_scale_list = has_ex2
    core.has_explicit_scale_only = has_ex3
    core._match_buffer_size = 1
    core._match_buffer = (core_module.NativeMatch * 1)()

    exports = {
        "wrpa_find_template": _FakeFunction(),
        "wrpa_find_template_ex": _FakeFunction(),
        "wrpa_find_template_ex2": _FakeFunction(),
        "wrpa_find_template_ex3": _FakeFunction(),
    }
    dll = mock.Mock()
    for name, func in exports.items():
        if name == "wrpa_find_template" or (
            name == "wrpa_find_template_ex" and has_ex
        ) or (name == "wrpa_find_template_ex2" and has_ex2) or (
            name == "wrpa_find_template_ex3" and has_ex3
        ):
            setattr(dll, name, func)
    core.dll = dll
    return core, exports


def _options() -> MatchOptions:
    return MatchOptions(
        image_path="template.png",
        regions=[(0, 0, 10, 10)],
        min_scale=0.8,
        max_scale=1.2,
        scale_step=0.1,
        use_gray=False,
        threshold=0.8,
        find_all=False,
        preferred_scale=1.0,
        preferred_scales=(1.0,),
        explicit_scale_only=False,
    )


class NativeCallArityTests(unittest.TestCase):
    """Each declared export must be invoked with exactly len(argtypes) args."""

    def test_ex_branch_matches_its_16_slot_argtypes(self) -> None:
        core, exports = _fake_core(has_ex=True, has_ex2=False, has_ex3=False)
        core._find_template_impl(_options())
        calls = exports["wrpa_find_template_ex"].calls
        self.assertEqual(len(calls), 1, "ex 分支应当被走到")
        self.assertEqual(
            len(calls[0]),
            16,
            "wrpa_find_template_ex 的 argtypes 为 16 位，实参必须一致",
        )

    def test_ex_eleventh_argument_is_a_scalar_scale(self) -> None:
        """Slot 11 is ``c_double``; a pointer there raises TypeError in ctypes."""
        core, exports = _fake_core(has_ex=True, has_ex2=False, has_ex3=False)
        core._find_template_impl(_options())
        eleventh = exports["wrpa_find_template_ex"].calls[0][10]
        self.assertNotIsInstance(eleventh, ctypes.Array)
        self.assertIsInstance(eleventh, float)

    def test_ex2_branch_keeps_17_arguments(self) -> None:
        """ex2 takes a preferred-scale array plus its length: 17 slots."""
        core, exports = _fake_core(has_ex=True, has_ex2=True, has_ex3=False)
        core._find_template_impl(_options())
        calls = exports["wrpa_find_template_ex2"].calls
        self.assertEqual(len(calls), 1, "ex2 分支应当被走到")
        self.assertEqual(len(calls[0]), 17)

    def test_ex3_branch_keeps_18_arguments(self) -> None:
        core, exports = _fake_core(has_ex=True, has_ex2=True, has_ex3=True)
        options = _options()
        options.explicit_scale_only = True
        core._find_template_impl(options)
        calls = exports["wrpa_find_template_ex3"].calls
        self.assertEqual(len(calls), 1, "ex3 分支应当被走到")
        self.assertEqual(len(calls[0]), 18)


if __name__ == "__main__":  # pragma: no cover - manual probe entry
    unittest.main()
