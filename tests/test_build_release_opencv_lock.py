"""Regression tests for S10: the site-packages cv2.pyd build lock.

``install_minimal_opencv`` mutates the developer's shared site-packages
``cv2.pyd``. S10 wraps the backup + replace in an advisory build lock so two
concurrent builds cannot race on that file, and documents the hard-kill
boundary (atexit restore is skipped on SIGKILL).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.build_release as br


class Cv2BuildLockTests(unittest.TestCase):
    def test_acquire_and_release(self):
        if not br._MSVCRT_AVAILABLE:
            self.skipTest("msvcrt unavailable (non-Windows)")
        import msvcrt

        with tempfile.TemporaryDirectory() as d:
            cv2_dir = Path(d)
            fd = br._acquire_cv2_build_lock(cv2_dir, timeout=2.0)
            self.assertIsInstance(fd, int)
            # A second non-blocking lock on the same file must fail while the
            # first is held.
            fd2 = os.open(str(cv2_dir / "cv2.pyd.fukua-buildlock"), os.O_RDWR)
            with self.assertRaises(OSError):
                msvcrt.locking(fd2, msvcrt.LK_NBLCK, 1)
            os.close(fd2)
            os.close(fd)  # release
            # After release a new lock succeeds.
            fd3 = br._acquire_cv2_build_lock(cv2_dir, timeout=2.0)
            self.assertIsInstance(fd3, int)
            os.close(fd3)

    def test_non_windows_returns_none(self):
        # The helper must degrade gracefully where msvcrt is absent.
        if br._MSVCRT_AVAILABLE:
            self.skipTest("msvcrt available on this platform")
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(br._acquire_cv2_build_lock(Path(d)))


class InstallMinimalOpenCVTests(unittest.TestCase):
    def _setup_fake(self, tmp: Path):
        # Minimal custom build that install_minimal_opencv will copy from.
        custom_dir = tmp / "opencv_build" / "lib" / "python3"
        custom_dir.mkdir(parents=True)
        custom = custom_dir / "cv2.pyd"
        custom.write_bytes(b"mini")
        # Fake site-packages cv2 package.
        cv2_dir = tmp / "site" / "cv2"
        cv2_dir.mkdir(parents=True)
        (cv2_dir / "__init__.py").write_text("")
        pip_pyd = cv2_dir / "cv2.pyd"
        pip_pyd.write_bytes(b"original")
        return custom, pip_pyd

    @mock.patch("sysconfig.get_config_var", return_value=".pyd")
    def test_replaces_when_custom_differs(self, mock_get_config_var):
        # Force a simple ``.pyd`` extension name so filenames are predictable.
        # install_minimal_opencv imports sysconfig lazily inside the function,
        # so we patch the stdlib module attribute directly rather than the
        # (absent) build_release.sysconfig attribute.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            custom, pip_pyd = self._setup_fake(tmp)
            spec = mock.MagicMock()
            spec.origin = str(tmp / "site" / "cv2" / "__init__.py")

            with mock.patch.object(br, "ROOT", tmp), mock.patch(
                "importlib.util.find_spec", return_value=spec
            ), mock.patch.object(
                br,
                "hash_file",
                side_effect=lambda p: "mini" if Path(str(p)).samefile(custom) else "orig",
            ), mock.patch.object(br, "shutil") as mock_shutil, mock.patch(
                "atexit.register"
            ) as mock_atexit:
                br.install_minimal_opencv()
                # The minimal build was copied over the pip one (a separate
                # copy2 call backs up the original to .bak first).
                mock_shutil.copy2.assert_any_call(custom, pip_pyd)
                # The original is scheduled for restoration at process exit.
                # mock_atexit is the mock for atexit.register itself, so the
                # call is mock_atexit(...), not mock_atexit.register(...).
                mock_atexit.assert_called_once_with(br.restore_pip_opencv)
                # The build lock was acquired and released (fd closed).
                self.assertTrue(mock_shutil.copy2.called)

    @mock.patch("sysconfig.get_config_var", return_value=".pyd")
    def test_no_replace_when_identical(self, mock_get_config_var):
        # See test_replaces_when_custom_differs for why we patch the stdlib
        # sysconfig attribute directly instead of scripts.build_release.sysconfig.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            custom, pip_pyd = self._setup_fake(tmp)
            spec = mock.MagicMock()
            spec.origin = str(tmp / "site" / "cv2" / "__init__.py")

            with mock.patch.object(br, "ROOT", tmp), mock.patch(
                "importlib.util.find_spec", return_value=spec
            ), mock.patch.object(
                br, "hash_file", return_value="same"
            ), mock.patch.object(br, "shutil") as mock_shutil, mock.patch(
                "atexit.register"
            ) as mock_atexit:
                br.install_minimal_opencv()
                mock_shutil.copy2.assert_not_called()
                mock_atexit.register.assert_not_called()

    def test_early_return_without_custom_build(self):
        # If the custom minimal build does not exist, nothing happens.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with mock.patch.object(br, "ROOT", tmp), mock.patch.object(
                br, "shutil"
            ) as mock_shutil, mock.patch("atexit.register") as mock_atexit:
                br.install_minimal_opencv()
                mock_shutil.copy2.assert_not_called()
                mock_atexit.register.assert_not_called()

    def test_backup_refreshed_when_pip_cv2_upgraded(self):
        """FR-20260812-D15-002: 升级 opencv 后 .bak 必须刷新，避免还原旧版。

        _backup_pip_cv2 在已有备份但内容与当前 pip cv2.pyd 不一致时（升级后）
        重新备份当前版本，而不是保留过时的旧备份。
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cv2_dir = tmp / "site" / "cv2"
            cv2_dir.mkdir(parents=True)
            pip_pyd = cv2_dir / "cv2.pyd"
            backup = cv2_dir / "cv2.pyd.bak"
            # 旧备份（升级前版本）
            backup.write_bytes(b"stale-backup")
            # 当前 pip 版本（升级后，与备份不同）
            pip_pyd.write_bytes(b"new-version")

            with mock.patch.object(
                br,
                "hash_file",
                side_effect=lambda p: (
                    "new" if str(Path(str(p))).endswith("cv2.pyd") else "stale"
                ),
            ):
                result = br._backup_pip_cv2(pip_pyd)
            self.assertEqual(result, backup)
            # 备份已被刷新为当前版本
            self.assertEqual(backup.read_bytes(), b"new-version")

    def test_backup_not_rewritten_when_identical(self):
        """FR-20260812-D15-002: 备份与当前 pip 版本一致时不重复覆盖。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cv2_dir = tmp / "site" / "cv2"
            cv2_dir.mkdir(parents=True)
            pip_pyd = cv2_dir / "cv2.pyd"
            backup = cv2_dir / "cv2.pyd.bak"
            backup.write_bytes(b"same")
            pip_pyd.write_bytes(b"same")

            with mock.patch.object(
                br, "hash_file", return_value="same"
            ), mock.patch.object(br, "shutil") as mock_shutil:
                result = br._backup_pip_cv2(pip_pyd)
            self.assertEqual(result, backup)
            mock_shutil.copy2.assert_not_called()

    def test_skip_when_build_lock_unavailable(self):
        # S10 concurrency branch: when the advisory lock cannot be acquired
        # (another build holds it / timed out), install_minimal_opencv must NOT
        # mutate site-packages. The function returns early instead of racing.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            custom, pip_pyd = self._setup_fake(tmp)
            spec = mock.MagicMock()
            spec.origin = str(tmp / "site" / "cv2" / "__init__.py")
            with mock.patch.object(br, "ROOT", tmp), mock.patch(
                "importlib.util.find_spec", return_value=spec
            ), mock.patch.object(
                br, "hash_file", side_effect=lambda p: "mini" if Path(str(p)).samefile(custom) else "orig"
            ), mock.patch.object(br, "shutil") as mock_shutil, mock.patch(
                "atexit.register"
            ) as mock_atexit, mock.patch.object(
                br, "_acquire_cv2_build_lock", return_value=None
            ):
                br.install_minimal_opencv()
                mock_shutil.copy2.assert_not_called()
                mock_atexit.register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
