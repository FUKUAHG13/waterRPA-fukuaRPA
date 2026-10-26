import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa import paths


class RuntimePathTests(unittest.TestCase):
    def tearDown(self):
        paths.runtime_path_info.cache_clear()

    def test_frozen_writable_directory_stays_portable(self):
        with tempfile.TemporaryDirectory() as install_dir, mock.patch.object(
            paths.sys, "frozen", True, create=True
        ), mock.patch.object(paths, "get_install_dir", return_value=install_dir), mock.patch.object(
            paths, "_directory_is_writable", return_value=True
        ):
            paths.runtime_path_info.cache_clear()
            info = paths.runtime_path_info()
            self.assertEqual(info.data_dir, install_dir)
            self.assertEqual(info.mode, "portable")

    def test_frozen_read_only_directory_falls_back_to_local_app_data(self):
        with tempfile.TemporaryDirectory() as root:
            install_dir = os.path.join(root, "install")
            local_dir = os.path.join(root, "local")
            os.makedirs(install_dir)
            with mock.patch.object(paths.sys, "frozen", True, create=True), mock.patch.object(
                paths, "get_install_dir", return_value=install_dir
            ), mock.patch.object(
                paths, "_directory_is_writable", return_value=False
            ):
                previous = os.environ.get("LOCALAPPDATA")
                os.environ["LOCALAPPDATA"] = local_dir
                try:
                    paths.runtime_path_info.cache_clear()
                    info = paths.runtime_path_info()
                    self.assertEqual(info.data_dir, os.path.join(local_dir, "fukuaRPA"))
                    self.assertEqual(info.mode, "local_app_data")
                finally:
                    if previous is None:
                        os.environ.pop("LOCALAPPDATA", None)
                    else:
                        os.environ["LOCALAPPDATA"] = previous

    def test_portable_flag_never_silently_moves_data(self):
        with tempfile.TemporaryDirectory() as install_dir:
            open(os.path.join(install_dir, paths.PORTABLE_FLAG_NAME), "w").close()
            with mock.patch.object(paths.sys, "frozen", True, create=True), mock.patch.object(
                paths, "get_install_dir", return_value=install_dir
            ), mock.patch.object(paths, "_directory_is_writable", return_value=False):
                paths.runtime_path_info.cache_clear()
                with self.assertRaisesRegex(RuntimeError, "portable.flag"):
                    paths.runtime_path_info()


if __name__ == "__main__":
    unittest.main()
