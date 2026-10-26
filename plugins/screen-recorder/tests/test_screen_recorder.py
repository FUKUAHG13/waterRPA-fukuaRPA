#!/usr/bin/env python3
"""屏幕录制插件单元测试（使用 Mock 后端，无需安装 mss/opencv/ffmpeg）。

运行
----
    python tests/test_screen_recorder.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from screen_recorder.action import execute_action, run  # noqa: E402
from screen_recorder.backend import (  # noqa: E402
    FfmpegBackend,
    MockRecorderBackend,
    MssOpencvBackend,
)
from screen_recorder.models import STATE_IDLE, STATE_RECORDING, RecorderStatus  # noqa: E402


class RecorderActionTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockRecorderBackend()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = self._tmp_dir.name

    def tearDown(self):
        self._tmp_dir.cleanup()

    def _exec(self, action_id, payload=None):
        return execute_action(
            "screen-recorder", action_id, payload or {},
            backend=self.backend, backend_name="mock",
        )

    def _out(self, name):
        return os.path.join(self.tmp, name)

    # ---- start ----
    def test_start_with_path(self):
        out = self._out("a.mp4")
        resp = self._exec("recorder_start", {"output_path": out})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["recording"])
        self.assertEqual(resp["result"]["output_path"], os.path.abspath(out))
        self.assertTrue(os.path.isfile(out))

    def test_start_with_output_dir(self):
        resp = self._exec("recorder_start", {"output_dir": self.tmp, "format": "mp4"})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["output_path"].endswith(".mp4"))

    def test_start_missing_target(self):
        resp = self._exec("recorder_start", {})
        self.assertFalse(resp["ok"])
        self.assertIn("output_path", resp["error"])

    def test_start_bad_fps(self):
        out = self._out("b.mp4")
        resp = self._exec("recorder_start", {"output_path": out, "fps": 0})
        self.assertFalse(resp["ok"])

    def test_start_bad_region(self):
        out = self._out("c.mp4")
        resp = self._exec("recorder_start", {"output_path": out, "region": {"x": 0, "y": 0}})
        self.assertFalse(resp["ok"])

    # ---- pause / resume ----
    def test_pause_resume(self):
        out = self._out("d.mp4")
        self._exec("recorder_start", {"output_path": out})
        r1 = self._exec("recorder_pause")
        self.assertTrue(r1["ok"])
        self.assertTrue(r1["result"]["paused"])
        r2 = self._exec("recorder_resume")
        self.assertTrue(r2["ok"])
        self.assertFalse(r2["result"]["paused"])
        self.assertTrue(r2["result"]["recording"])

    def test_pause_without_start(self):
        resp = self._exec("recorder_pause")
        self.assertFalse(resp["ok"])

    def test_resume_without_pause(self):
        out = self._out("e.mp4")
        self._exec("recorder_start", {"output_path": out})
        resp = self._exec("recorder_resume")
        self.assertFalse(resp["ok"])

    # ---- stop ----
    def test_stop(self):
        out = self._out("f.mp4")
        self._exec("recorder_start", {"output_path": out})
        r = self._exec("recorder_stop")
        self.assertTrue(r["ok"])
        self.assertFalse(r["result"]["recording"])
        self.assertGreaterEqual(r["result"]["duration_ms"], 0)
        self.assertGreater(r["result"]["file_bytes"], 0)

    def test_stop_without_start(self):
        resp = self._exec("recorder_stop")
        self.assertFalse(resp["ok"])

    # ---- snapshot ----
    def test_snapshot_with_path(self):
        p = self._out("shot.png")
        resp = self._exec("recorder_snapshot", {"path": p})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["path"], os.path.abspath(p))
        self.assertTrue(os.path.isfile(p))

    def test_snapshot_with_output_dir(self):
        resp = self._exec("recorder_snapshot", {"output_dir": self.tmp})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["path"].endswith(".png"))

    def test_snapshot_missing_target(self):
        resp = self._exec("recorder_snapshot", {})
        self.assertFalse(resp["ok"])

    # ---- unknown / envelope ----
    def test_unknown_action(self):
        resp = self._exec("recorder_nope", {})
        self.assertFalse(resp["ok"])
        self.assertIn("未知动作", resp["error"])

    def test_run_envelope_dispatch(self):
        out = self._out("g.mp4")
        request = {
            "protocol": "fukuaRPA.plugin",
            "version": 1,
            "kind": "request",
            "payload": {
                "run_id": "r1", "step_id": "s1", "plugin_id": "screen-recorder",
                "action_id": "recorder_start", "deadline_ns": 0,
                "payload": {"output_path": out},
            },
        }
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["recording"])

    def test_run_unknown_action_via_envelope(self):
        request = {"payload": {"action_id": "recorder_unknown", "payload": {}}}
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertFalse(resp["ok"])

    def test_run_exception_is_caught(self):
        # 非法 region 经 run() 也应被捕获并转成失败响应，绝不外抛。
        request = {"payload": {"action_id": "recorder_start", "payload": {"output_path": self._out("h.mp4"), "region": {"x": 1}}}}
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertFalse(resp["ok"])

    def test_full_session_lifecycle(self):
        # 完整会话 start→pause→resume→stop 串联（Mock 后端）。
        out = self._out("life.mp4")
        self._exec("recorder_start", {"output_path": out})
        self._exec("recorder_pause")
        self._exec("recorder_resume")
        r = self._exec("recorder_stop")
        self.assertTrue(r["ok"])
        self.assertFalse(r["result"]["recording"])
        self.assertTrue(os.path.isfile(out))


class MssStopValidationTests(unittest.TestCase):
    """MssOpencvBackend.stop() 的取证完整性校验（无需 cv2/mss，构造层惰性导入）。"""

    def _scenario(self, frames: int, errors: int) -> MssOpencvBackend:
        b = MssOpencvBackend()
        b._status = RecorderStatus(
            state=STATE_RECORDING,
            output_path=None,
            started_at_ns=time.time_ns(),
            frames=frames,
        )
        b._capture_errors = errors
        b._thread = None
        return b

    def test_stop_raises_on_zero_frames(self):
        # P2#4：未捕获任何帧不得伪装成成功。
        b = self._scenario(0, 0)
        with self.assertRaises(RuntimeError):
            b.stop()

    def test_stop_raises_on_capture_errors(self):
        # P2#4：逐帧采集有失败应明确报错而非静默 ok。
        b = self._scenario(5, 2)
        with self.assertRaises(RuntimeError):
            b.stop()

    def test_stop_ok_when_frames_captured(self):
        b = self._scenario(10, 0)
        st = b.stop()
        self.assertEqual(st.state, STATE_IDLE)
        self.assertEqual(st.frames, 10)

    def test_stop_failure_cleans_up_own_output(self):
        """FR-20260812-D8-001: stop 失败（0 帧）必须删除本方自建的空文件，
        使同路径重试不命中 exists SAFETY 守卫。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "empty.mp4")
            # 模拟 start() 自建文件：录制前不存在 → _created_output=True
            b = MssOpencvBackend()
            b._created_output = True
            b._status = RecorderStatus(
                state=STATE_RECORDING,
                output_path=out,
                started_at_ns=time.time_ns(),
                frames=0,
            )
            b._capture_errors = 0
            b._thread = None
            open(out, "wb").close()  # 空文件（0 帧产物）
            with self.assertRaises(RuntimeError):
                b.stop()
            self.assertFalse(os.path.exists(out), "stop 失败后自建空文件应被清理")

    def test_stop_failure_keeps_pre_existing_output(self):
        """FR-20260812-D8-001: start 前已存在的用户文件（_created_output=False）
        在 stop 失败时绝不能被删除。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "user_file.mp4")
            open(out, "wb").write(b"pre-existing")
            b = MssOpencvBackend()
            b._created_output = False  # 用户既有文件
            b._status = RecorderStatus(
                state=STATE_RECORDING,
                output_path=out,
                started_at_ns=time.time_ns(),
                frames=0,
            )
            b._capture_errors = 0
            b._thread = None
            with self.assertRaises(RuntimeError):
                b.stop()
            self.assertTrue(os.path.exists(out), "用户既有文件不得被删除")
            self.assertEqual(open(out, "rb").read(), b"pre-existing")


class FfmpegBackendContractTests(unittest.TestCase):
    """ffmpeg 后端对不支持的暂停/恢复必须明确报错（设计内行为）。"""

    def test_pause_unsupported(self):
        with self.assertRaises(RuntimeError):
            FfmpegBackend().pause()

    def test_resume_unsupported(self):
        with self.assertRaises(RuntimeError):
            FfmpegBackend().resume()


class FfmpegStopValidationTests(unittest.TestCase):
    """修复 P2：ffmpeg 静默失败时 stop() 不得伪装成成功（0 字节/缺失文件抛错）。"""

    class _FakeProc:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

        def kill(self):
            pass

    def _backend(self, output_path):
        b = FfmpegBackend()
        b._proc = self._FakeProc()
        b._status = RecorderStatus(
            state=STATE_RECORDING, output_path=output_path, started_at_ns=time.time_ns()
        )
        return b

    def test_stop_raises_when_output_missing(self):
        b = self._backend(os.path.join(tempfile.gettempdir(), "no_such_file.mp4"))
        with self.assertRaises(RuntimeError):
            b.stop()

    def test_snapshot_timeout_raises_runtime_error(self):
        """FR-20260809-1203: ffmpeg 挂起超时必须转 RuntimeError，不无限阻塞。"""
        import subprocess

        b = FfmpegBackend()
        with mock.patch.object(b, "available", return_value=True), mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
        ), self.assertRaises(RuntimeError) as ctx:
            b.snapshot(os.path.join(tempfile.gettempdir(), "out.png"), None)
        self.assertIn("超时", str(ctx.exception))
        self.assertIsNone(b._proc)

    def test_stop_raises_on_zero_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "empty.mp4")
            open(out, "wb").close()  # 0 字节
            b = self._backend(out)
            with self.assertRaises(RuntimeError):
                b.stop()

    def test_stop_ok_when_file_present(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "real.mp4")
            with open(out, "wb") as handle:
                handle.write(b"mock-video-bytes")
            b = self._backend(out)
            st = b.stop()
            self.assertEqual(st.state, STATE_IDLE)
            self.assertGreater(st.file_bytes, 0)


class AutoCloseOwnershipTests(unittest.TestCase):
    """FR-a01-001（与 browser-automation 的 FR-20260813-D11-001 同型修复）：
    传入 backend 且 auto_close=True 时必须关闭，不得因 own_backend 守卫漏关
    （CLI 自建 backend 未入 _BACKEND_CACHE，漏关会逃过 atexit 造成 ffmpeg 子进程
    泄漏）。run() 宿主路径不传 auto_close，不受影响。"""

    def _exec(self, action_id, payload, backend, auto_close):
        return execute_action(
            "screen-recorder", action_id, payload,
            backend=backend, backend_name="mock", auto_close=auto_close,
        )

    def test_passed_in_backend_closed_on_auto_close(self):
        with tempfile.TemporaryDirectory() as d:
            backend = MockRecorderBackend()
            backend.close = mock.Mock(wraps=backend.close)
            resp = self._exec(
                "recorder_start",
                {"output_path": os.path.join(d, "a.mp4")},
                backend, auto_close=True,
            )
            self.assertTrue(resp["ok"])
            backend.close.assert_called_once()

    def test_passed_in_backend_kept_when_auto_close_false(self):
        with tempfile.TemporaryDirectory() as d:
            backend = MockRecorderBackend()
            backend.close = mock.Mock(wraps=backend.close)
            resp = self._exec(
                "recorder_start",
                {"output_path": os.path.join(d, "b.mp4")},
                backend, auto_close=False,
            )
            self.assertTrue(resp["ok"])
            backend.close.assert_not_called()

    def test_run_host_path_does_not_close_shared_backend(self):
        with tempfile.TemporaryDirectory() as d:
            backend = MockRecorderBackend()
            backend.close = mock.Mock(wraps=backend.close)
            request = {
                "payload": {
                    "run_id": "r1",
                    "step_id": "s1",
                    "plugin_id": "screen-recorder",
                    "action_id": "recorder_start",
                    "payload": {"output_path": os.path.join(d, "c.mp4")},
                }
            }
            run(request, backend=backend, backend_name="mock")
            backend.close.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
