"""启动测量协议组件单测（阶段 1）。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from scripts import startup_measurement as sm


def _write_py(path: Path, text: str = "VALUE = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _compile(py_path: Path) -> Path:
    import py_compile

    return Path(py_compile.compile(str(py_path), doraise=True))


class ClassifyTests(unittest.TestCase):
    def test_frozen_is_its_own_class(self):
        self.assertEqual(
            sm.classify_measurement(kind="frozen", cache_ok=None), "startup_frozen"
        )

    def test_source_requires_cache_verdict(self):
        self.assertEqual(sm.classify_measurement(kind="source"), "unknown")
        self.assertEqual(
            sm.classify_measurement(kind="source", cache_ok=True),
            "startup_source_cached",
        )
        self.assertEqual(
            sm.classify_measurement(kind="source", cache_ok=False),
            "startup_source_nocache",
        )


class CacheValidationTests(unittest.TestCase):
    def test_valid_cache_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            py_path = _write_py(root / "pkg" / "mod.py")
            _compile(py_path)
            result = sm.validate_cache([py_path], runtime_file=str(py_path), copy_root=root)
            self.assertTrue(result.ok, result.as_dict())
            self.assertEqual(result.checked, 1)

    def test_missing_cache_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            py_path = _write_py(root / "pkg" / "mod.py")
            result = sm.validate_cache([py_path])
            self.assertFalse(result.ok)
            self.assertIn("missing", result.reasons)

    def test_stale_cache_detected_via_header(self):
        """手工构造头内嵌 mtime/size 不匹配的 pyc，必须判 stale。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            py_path = _write_py(root / "pkg" / "mod.py")
            cache_path = Path(importlib.util.cache_from_source(str(py_path)))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(
                importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little") + (1).to_bytes(4, "little")
                + b"payload"
            )
            result = sm.validate_cache([py_path])
            self.assertFalse(result.ok)
            self.assertIn("stale", result.reasons)

    def test_wrong_magic_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            py_path = _write_py(root / "pkg" / "mod.py")
            cache_path = Path(importlib.util.cache_from_source(str(py_path)))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"\x00\x00\x00\x00" + b"\x00" * 12)
            result = sm.validate_cache([py_path])
            self.assertFalse(result.ok)
            self.assertIn("magic", result.reasons)

    def test_runtime_outside_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as other:
            root = Path(temp_dir)
            py_path = _write_py(root / "pkg" / "mod.py")
            _compile(py_path)
            outside = Path(other) / "pkg" / "mod.py"
            result = sm.validate_cache(
                [py_path], runtime_file=str(outside), copy_root=root
            )
            self.assertFalse(result.ok)
            self.assertIn("runtime_outside_copy", result.reasons)


class SamplingTests(unittest.TestCase):
    def test_five_samples_median(self):
        values = iter([100, 110, 120, 130, 140])
        result = sm.collect_samples(lambda: next(values), reference=120)
        self.assertEqual(result.samples, [100, 110, 120, 130, 140])
        self.assertEqual(result.median, 120)
        self.assertFalse(result.unstable)

    def test_outlier_round_is_discarded_and_kept_in_evidence(self):
        rounds = [[100, 110, 900, 130, 140], [100, 110, 120, 130, 140]]
        queue = list(rounds)

        def run_once():
            return queue[0].pop(0) if queue[0] else queue.pop(0)

        values = iter(rounds[0] + rounds[1])
        result = sm.collect_samples(lambda: next(values), reference=120)
        self.assertEqual(result.median, 120)
        self.assertTrue(result.discarded)
        self.assertIn("upper_outlier", result.contamination)
        self.assertEqual(result.discarded[0]["samples"], rounds[0])

    def test_error_marks_round_contaminated_and_stops_after_one_retry(self):
        calls = {"n": 0}

        def run_once():
            calls["n"] += 1
            raise RuntimeError("boom")

        result = sm.collect_samples(run_once, reference=120)
        self.assertTrue(result.unstable)
        self.assertTrue(any(r.startswith("error:") for r in result.contamination))
        # 出错即中止该轮：首轮 1 次 + 重采 1 次（重采上限 1 次）
        self.assertEqual(calls["n"], 2)


class IsolatedCopyTests(unittest.TestCase):
    def test_copy_excludes_bytecode(self):
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            src = Path(src_dir)
            py_path = _write_py(src / "pkg" / "mod.py")
            _compile(py_path)
            self.assertTrue(list(src.rglob("*.pyc")))
            copy = sm.build_isolated_copy(src, Path(dst_dir) / "copy", precompile=False)
            self.assertFalse(list(copy.rglob("*.pyc")))
            self.assertTrue((copy / "pkg" / "mod.py").is_file())

    def test_precompiled_copy_passes_validation(self):
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            src = Path(src_dir)
            _write_py(src / "pkg" / "mod.py")
            copy = sm.build_isolated_copy(src, Path(dst_dir) / "copy", precompile=True)
            py_files = list((copy / "pkg").rglob("*.py"))
            result = sm.validate_cache(py_files)
            self.assertTrue(result.ok, result.as_dict())

    def test_clean_env_strips_python_overrides(self):
        import os

        os.environ["PYTHONPATH"] = "x"
        try:
            env = sm.clean_python_env()
            self.assertNotIn("PYTHONPATH", env)
        finally:
            os.environ.pop("PYTHONPATH", None)


if __name__ == "__main__":
    unittest.main()
