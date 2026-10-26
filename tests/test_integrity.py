import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fukua_rpa.integrity import (
    PAYLOAD_MANIFEST_NAME,
    atomic_write_manifest,
    build_payload_manifest,
    verify_payload,
)


class PayloadIntegrityTests(unittest.TestCase):
    def create_release(self, root: Path):
        (root / "_internal").mkdir()
        (root / "app.exe").write_bytes(b"exe")
        (root / "_internal" / "runtime.dll").write_bytes(b"dll")
        manifest = build_payload_manifest(root)
        atomic_write_manifest(root / PAYLOAD_MANIFEST_NAME, manifest)
        return manifest

    def test_clean_payload_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.create_release(root)
            report = verify_payload(root)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["checked"], manifest["file_count"])

    def test_changed_file_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_release(root)
            (root / "app.exe").write_bytes(b"changed")
            report = verify_payload(root)
            self.assertFalse(report["ok"])
            self.assertIn("app.exe", report["mismatched"])

    def test_extra_log_is_warning_but_extra_dll_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_release(root)
            (root / "runtime.log").write_text("log", encoding="utf-8")
            report = verify_payload(root)
            self.assertTrue(report["ok"], report)
            self.assertIn("runtime.log", report["unexpected"])
            (root / "injected.dll").write_bytes(b"dll")
            report = verify_payload(root)
            self.assertFalse(report["ok"])
            self.assertIn("injected.dll", report["suspicious_unexpected"])

    def test_common_windows_executable_suffixes_fail_integrity_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_release(root)
            # .pyw（pythonw 关联双击即可执行）与 .chm（内嵌可执行 HTML）同属
            # 清单外可执行内容，必须与 .py/.pyd 同等阻断（SEC-FR-002）。
            for suffix in (".com", ".cmd", ".scr", ".ps1", ".lnk", ".pyw", ".chm"):
                candidate = root / f"unexpected{suffix}"
                candidate.write_bytes(b"unexpected")
                with self.subTest(suffix=suffix):
                    report = verify_payload(root)
                    self.assertFalse(report["ok"])
                    self.assertIn(
                        candidate.name, report["suspicious_unexpected"]
                    )
                candidate.unlink()

    def test_unsafe_manifest_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "format": "fukuaRPA_payload_hashes",
                "files": [{"path": "../outside.dll", "size": 0, "sha256": ""}],
            }
            (root / PAYLOAD_MANIFEST_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            report = verify_payload(root)
            self.assertFalse(report["ok"])
            self.assertIn("不安全路径", report["error"])

    def test_malformed_manifest_entry_returns_failure_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "format": "fukuaRPA_payload_hashes",
                "files": ["not-an-object"],
            }
            (root / PAYLOAD_MANIFEST_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )

            report = verify_payload(root)

            self.assertFalse(report["ok"])
            self.assertIn("条目格式", report["error"])

    def test_duplicate_manifest_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / PAYLOAD_MANIFEST_NAME).write_text(
                '{"format":"fukuaRPA_payload_hashes",'
                '"format":"fukuaRPA_payload_hashes","files":[]}',
                encoding="utf-8",
            )

            report = verify_payload(root)

            self.assertFalse(report["ok"])
            self.assertIn("重复字段", report["error"])

    def test_actual_directory_scan_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_release(root)
            (root / "extra.log").write_text("log", encoding="utf-8")

            with mock.patch("fukua_rpa.integrity.MAX_PAYLOAD_FILES", 2):
                report = verify_payload(root)

            self.assertFalse(report["ok"])
            self.assertIn("文件数量超过上限", report["error"])

    def test_non_object_manifest_returns_failure_instead_of_raising(self):
        """P3 regression: a manifest that is valid JSON but not an object
        (e.g. [] or null) must return a failure report, not raise AttributeError."""
        for content in ("[]", "null", "0", '"string"'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / PAYLOAD_MANIFEST_NAME).write_text(
                    content, encoding="utf-8"
                )
                report = verify_payload(root)
                self.assertFalse(report["ok"])
                self.assertIn("清单根结构必须是对象", report["error"])


if __name__ == "__main__":
    unittest.main()
