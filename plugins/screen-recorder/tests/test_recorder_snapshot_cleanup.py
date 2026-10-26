#!/usr/bin/env python3
"""D9-001 回归：snapshot 中途失败应清理残缺文件，避免 SAFETY 守卫永久阻止同路径重试。

运行
----
    python tests/test_recorder_snapshot_cleanup.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from screen_recorder.backend import FfmpegBackend, MssOpencvBackend  # noqa: E402


class _PartialWriteFakeMss:
    """模拟 mss：to_png 写出部分字节后失败，使 snapshot 进入异常清理分支。

    _full_bbox() 与 snapshot() 都会 ``import mss`` 并 ``with mss.mss()``，
    因此 fake 的上下文管理器需提供 ``monitors``。
    """

    class mss:
        def __init__(self):
            self.monitors = [{}, {"left": 0, "top": 0, "width": 1920, "height": 1080}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, bbox):
            return type("Img", (), {"rgb": b"\x00" * 100, "size": (10, 10)})()

    class tools:
        @staticmethod
        def to_png(rgb, size, output):
            with open(output, "wb") as fh:
                fh.write(b"partial-bytes")
            raise IOError("simulated encode failure")


class SnapshotCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_mss_snapshot_cleans_partial_on_failure(self):
        backend = MssOpencvBackend()
        path = os.path.join(self.tmp, "shot.png")
        with mock.patch.dict(sys.modules, {"mss": _PartialWriteFakeMss}):
            with self.assertRaises(Exception):
                backend.snapshot(path, None)
        self.assertFalse(os.path.isfile(path), "snapshot 失败后残缺文件应被清理")

    def test_ffmpeg_snapshot_cleans_partial_on_timeout(self):
        backend = FfmpegBackend()
        path = os.path.join(self.tmp, "shot.png")
        captured = {"path": path}

        def _fake_run(cmd, **kw):
            import subprocess

            # 模拟 ffmpeg 在超时前已写出部分文件（不预先写文件，避免触发 SAFETY 守卫）。
            with open(captured["path"], "wb") as fh:
                fh.write(b"partial")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        with mock.patch.object(FfmpegBackend, "available", return_value=True):
            with mock.patch("subprocess.run", side_effect=_fake_run):
                with self.assertRaises(RuntimeError):
                    backend.snapshot(path, None)
        self.assertFalse(os.path.isfile(path), "ffmpeg 超时后残缺文件应被清理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
