"""FR-101-001 回归测试：启动入口对 get_base_dir() 早期异常的可见兜底。

冻结包（console=False）下，portable.flag+只读目录或 %LOCALAPPDATA% 创建失败时
runtime_path_info 会抛异常；若入口不捕获，进程静默退出且无提示。修复后应弹出
可见错误并返回非零码（与 os.makedirs/SingleInstanceGuard 的既有兜底一致）。
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fukuaRPA  # noqa: E402


class StartupBaseDirGuardTests(unittest.TestCase):
    """FR-101-001: get_base_dir()/runtime_path_info() 异常必须被捕获并返回 4。"""

    def _call_main_with_base_dir_failure(self, exc):
        with mock.patch.object(fukuaRPA, "get_base_dir", side_effect=exc), mock.patch.object(
            fukuaRPA.ctypes.windll.user32, "MessageBoxW"
        ) as mb, mock.patch.object(sys, "argv", ["fukuaRPA.py"]):
            code = fukuaRPA.main()
        return code, mb

    def test_runtime_error_returns_4_and_shows_message(self):
        code, mb = self._call_main_with_base_dir_failure(
            RuntimeError("portable 目录只读，无法确定数据目录")
        )
        self.assertEqual(code, 4)
        self.assertTrue(mb.called)

    def test_oserror_returns_4_and_shows_message(self):
        code, mb = self._call_main_with_base_dir_failure(
            OSError("LOCALAPPDATA 不可写")
        )
        self.assertEqual(code, 4)
        self.assertTrue(mb.called)


if __name__ == "__main__":
    unittest.main()
