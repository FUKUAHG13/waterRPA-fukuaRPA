"""FR-20261023-STARTUP-B-002：测量副本不得删除或改写输入与旧证据。"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_startup_ab as ab


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class StartupCopySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.reference = self.make_source(self.root / "reference", "reference")
        self.candidate = self.make_source(self.root / "candidate", "candidate")
        self.dll = self.root / "input.dll"
        self.dll.write_bytes(b"TEST_DLL_BYTES")

    def make_source(self, root, label):
        for name in ("fukua_rpa", "assets", "plugins"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "fukua_rpa/marker.py").write_text(f"MARKER = {label!r}\n", encoding="utf-8")
        (root / "fukuaRPA.py").write_text("# 不执行的测试标记\n", encoding="utf-8")
        (root / "assets/marker.txt").write_bytes(label.encode())
        return root

    def call_main(self, *, out, reference=None, candidate=None, conditions="a,b", first_open=False):
        args = ["run_startup_ab.py", "--reference", str(reference or self.reference),
                "--candidate", str(candidate or self.candidate), "--out", str(out),
                "--dll", str(self.dll), "--conditions", conditions, "--pairs", "2"]
        if first_open:
            args.append("--first-open")
        with mock.patch.object(sys, "argv", args):
            return ab.main()

    def test_direct_collision_never_changes_source_or_existing_destination(self):
        for kind in ("same", "ancestor", "existing", "dll_parent"):
            with self.subTest(kind=kind):
                case = self.root / kind
                source = self.make_source(case / "source", kind)
                dll = case / "dll/input.dll"
                dll.parent.mkdir()
                dll.write_bytes(b"LOCAL_DLL")
                destination = {"same": source, "ancestor": case, "existing": case / "old", "dll_parent": dll.parent}[kind]
                if kind == "existing":
                    destination.mkdir()
                    (destination / "evidence.json").write_bytes(b"DO_NOT_REMOVE")
                before = files(case)
                error = None
                try:
                    ab.build_copy(source, destination, precompile=False, dll=dll)
                except (ValueError, FileExistsError) as exc:
                    error = exc
                self.assertEqual(files(case), before, "拒绝危险复制不能删除源或旧证据")
                self.assertIsNotNone(error, "危险路径必须明确拒绝")

    def test_main_preflight_protects_both_arms_dll_and_first_open_before_writes(self):
        for kind in ("reference", "candidate_later", "first_open", "out_in_source", "dll", "nonempty", "same_root"):
            with self.subTest(kind=kind):
                case = self.root / ("cli_" + kind)
                out = case / "out"
                reference, candidate = self.reference, self.candidate
                if kind in ("reference", "candidate_later", "first_open"):
                    source = self.make_source(out / {"reference": "ref-A", "candidate_later": "cand-B", "first_open": "first-open-cand-2"}[kind], kind)
                    if kind == "reference":
                        reference = source
                    else:
                        candidate = source
                elif kind == "out_in_source":
                    reference = self.make_source(case / "source", kind)
                    out = reference / "fukua_rpa/measurement"
                elif kind == "dll":
                    out.mkdir(parents=True)
                    self.dll = out / "input.dll"
                    self.dll.write_bytes(b"DLL_INSIDE_OUTPUT")
                elif kind == "nonempty":
                    out.mkdir(parents=True)
                    (out / "session.json").write_bytes(b"EXISTING_EVIDENCE")
                else:
                    reference = self.make_source(out, kind)
                before = files(self.root)
                existed = out.exists()
                error = None
                # 旧版若未预检，不让它递归复制到源内部；真实main的写入时序仍保留。
                with mock.patch.object(ab, "build_copy", side_effect=AssertionError("必须先路径预检")) as copier:
                    try:
                        self.call_main(out=out, reference=reference, candidate=candidate, first_open=True)
                    except (ValueError, FileExistsError, SystemExit, AssertionError) as exc:
                        error = exc
                    self.assertEqual(files(self.root), before, "拒绝前不得写session或破坏输入")
                    self.assertEqual(out.exists(), existed, "拒绝前不得创建输出目录")
                    copier.assert_not_called()
                self.assertIsInstance(error, (ValueError, FileExistsError, SystemExit))

    def test_invalid_conditions_are_rejected_before_output_creation(self):
        for conditions in ("", "a,a", "a,../escape", "c", "a,"):
            with self.subTest(conditions=conditions):
                out = self.root / "invalid_output"
                with mock.patch.object(ab, "build_copy", side_effect=AssertionError("非法条件不应复制")) as copier:
                    with self.assertRaises((ValueError, SystemExit)):
                        self.call_main(out=out, conditions=conditions)
                    copier.assert_not_called()
                self.assertFalse(out.exists())

    def test_resolved_windows_junction_collision_is_rejected(self):
        alias = self.root / "reference_alias"
        # Windows目录联接无需创建符号链接权限；只指向本测试自己的标记目录。
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.reference)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(alias.resolve(), self.reference)
        before = files(self.reference)
        error = None
        try:
            ab.build_copy(self.reference, alias, precompile=False, dll=self.dll)
        except (ValueError, FileExistsError) as exc:
            error = exc
        self.assertEqual(files(self.reference), before)
        self.assertIsNotNone(error)

    def test_normal_new_copy_keeps_whitelist_and_dll_bytes(self):
        cache = self.reference / "fukua_rpa/__pycache__"
        cache.mkdir()
        (cache / "ignored.pyc").write_bytes(b"CACHE")
        (self.reference / "config.ini").write_bytes(b"NOT_COPIED")
        before = files(self.reference)
        out = self.root / "fresh"
        actual = ab.build_copy(self.reference, out, precompile=False, dll=self.dll)
        self.assertEqual(actual, out)
        self.assertEqual(files(self.reference), before)
        self.assertEqual((out / "fukua_rpa_core.dll").read_bytes(), self.dll.read_bytes())
        self.assertEqual((out / "fukua_rpa/marker.py").read_bytes(), (self.reference / "fukua_rpa/marker.py").read_bytes())
        self.assertFalse((out / "config.ini").exists())
        self.assertFalse(list(out.rglob("*.pyc")))

    def test_source_junction_cannot_recurse_into_output(self):
        parent = self.root / "linked_output_parent"
        parent.mkdir()
        alias = self.reference / "fukua_rpa/output_alias"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(parent)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        out = parent / "fresh"
        error = None
        with mock.patch.object(ab.shutil, "copytree", side_effect=AssertionError("不能递归到输出")) as copier:
            try:
                ab.build_copy(self.reference, out, precompile=False, dll=self.dll)
            except (ValueError, AssertionError) as exc:
                error = exc
            self.assertFalse(out.exists(), "拒绝源内联接必须早于目标创建")
            copier.assert_not_called()
        self.assertIsInstance(error, ValueError)

    def test_normal_main_preserves_pair_cache_and_first_open_contracts(self):
        out = self.root / "normal_cli"
        metrics = dict.fromkeys(ab.METRICS, 500.0)
        first = {"row_ms": 1.0, "first_open_ms": 2.0, "dialog": "TaskConfigDialog"}
        before = files(self.root)
        with (mock.patch.object(ab, "run_smoke", return_value=metrics) as smoke,
              mock.patch.object(ab, "measure_first_open", return_value=first) as opening,
              mock.patch.object(ab, "validate_b_condition", return_value={"validation": {"ok": True}}) as cache,
              mock.patch.object(ab.subprocess, "run") as compile_run):
            self.assertEqual(self.call_main(out=out, first_open=True), 0)
        self.assertEqual(smoke.call_count, 8)
        self.assertEqual(opening.call_count, 4)
        self.assertEqual(cache.call_count, 2)
        self.assertEqual(compile_run.call_count, 2)
        results = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(set(results), {"a", "b"})
        for payload in results.values():
            self.assertEqual(len(payload["pairs"]), 2)
            self.assertEqual([p["order"] for p in payload["pairs"]], [["ref", "cand"], ["cand", "ref"]])
            self.assertFalse(payload["unstable"])
        self.assertEqual({p: files(self.root)[p] for p in before}, before)


if __name__ == "__main__":
    unittest.main()
