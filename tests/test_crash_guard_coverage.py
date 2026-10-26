"""定向覆盖 crash_guard.py 的未覆盖路径。

与 tests/test_crash_guard.py 互补，专注其未触及的分支：

* 文本/时间辅助函数的异常回退（_epoch_to_text / _safe_float / _safe_detail /
  _exit_code_text / _windows_version_text / _arch_text / _now_text）；
* 状态读写防御分支（_read_state 超大文件、非 dict、损坏 JSON；
  record_breadcrumb / mark_clean_exit 写入失败静默）；
* install_fault_handler 关闭旧句柄与失败回退；
* launch_watchdog 冻结包分支、成功路径、空 argv 回退；
* _format_crash_report 跳过非 dict 面包屑；_write_crash_report 失败与临时文件清理；
* _wait_for_parent 的 ACCESS_DENIED 轮询、PID 复用检测、句柄关闭异常、
  GetExitCodeProcess 失败、顶层异常，以及 _get_process_creation_time；
* _watchdog_run 的等待异常、sleep 中断、writer 异常、默认 wait_func 分支；
* watchdog_main 顶层异常回退。

所有测试均离屏：mock 子进程 / Win32 进程 API，状态文件用 tempfile 隔离，
看门狗循环用 mock 控制迭代次数避免真实等待。
"""

import contextlib
import ctypes
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from fukua_rpa import crash_guard

# --- 文本 / 时间辅助函数 ----------------------------------------------------


class TimeTextHelperTests(unittest.TestCase):
    def test_now_text_returns_formatted_string(self):
        text = crash_guard._now_text()
        self.assertIsInstance(text, str)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_epoch_to_text_valid(self):
        text = crash_guard._epoch_to_text(1_700_000_000)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}")

    def test_epoch_to_text_invalid_falls_back(self):
        # float(None) 触发 TypeError → 回退占位文本。
        self.assertEqual(crash_guard._epoch_to_text(None), "未知")
        self.assertEqual(crash_guard._epoch_to_text("not-a-number"), "未知")

    def test_safe_float_valid_and_invalid(self):
        self.assertEqual(crash_guard._safe_float("1.5"), 1.5)
        self.assertIsNone(crash_guard._safe_float(None))
        self.assertIsNone(crash_guard._safe_float("abc"))

    def test_bounded_token_strips_and_truncates(self):
        self.assertEqual(crash_guard._bounded_token("  a\r\nb  "), "a  b")
        long = "x" * 200
        self.assertEqual(len(crash_guard._bounded_token(long)), crash_guard._MAX_STAGE_CHARS)

    def test_bounded_token_redacts_paths_fail_closed(self):
        """FR-20260809-D6-④: stage 字段纵深防御——即使调用方把路径放进 stage，
        崩溃报告（始终脱敏）也不得原样落盘；脱敏失败返回占位符（fail-closed）。"""
        from unittest import mock

        with mock.patch("fukua_rpa.crash_guard.redact_sensitive_paths") as redact:
            redact.side_effect = RuntimeError("redact fail")
            self.assertEqual(
                crash_guard._bounded_token("stage-name"), "[stage unavailable]"
            )
        # 正常路径：路径被脱敏。
        self.assertNotIn(
            "C:\\Users\\me", crash_guard._bounded_token("C:\\Users\\me\\stage")
        )
        self.assertEqual(
            crash_guard._bounded_token("普通阶段名"), "普通阶段名"
        )

    def test_safe_detail_empty(self):
        self.assertEqual(crash_guard._safe_detail(""), "")
        self.assertEqual(crash_guard._safe_detail(None), "")

    def test_safe_detail_redaction_failure_uses_placeholder(self):
        # 脱敏函数抛错时绝不回退到未脱敏文本，而用占位符。
        with mock.patch.object(
            crash_guard, "plain_log_text", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(crash_guard._safe_detail("anything"), "[detail unavailable]")


class ExitCodeTextBranchTests(unittest.TestCase):
    def test_non_numeric_value_returns_str(self):
        # int("abc") 触发 ValueError → 直接返回 str(value)。
        self.assertEqual(crash_guard._exit_code_text("abc"), "abc")

    def test_large_positive_code_shows_hex(self):
        text = crash_guard._exit_code_text(0x80000000)
        self.assertIn("80000000", text)


class PlatformTextFailureTests(unittest.TestCase):
    def test_windows_version_failure_falls_back(self):
        with mock.patch.object(sys, "getwindowsversion", side_effect=AttributeError("nope")):
            self.assertEqual(crash_guard._windows_version_text(), "未知")

    def test_arch_failure_falls_back(self):
        with mock.patch.object(crash_guard.struct, "calcsize", side_effect=ValueError("nope")):
            self.assertEqual(crash_guard._arch_text(), "未知")


# --- 状态文件读写防御分支 ----------------------------------------------------


class ReadStateGuardTests(unittest.TestCase):
    def test_oversized_file_returns_none(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(" " * (crash_guard._MAX_STATE_BYTES + 16))
            self.assertIsNone(crash_guard._read_state(path))

    def test_non_dict_json_returns_none(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([1, 2, 3], handle)
            self.assertIsNone(crash_guard._read_state(path))

    def test_corrupt_json_returns_none(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not valid json")
            self.assertIsNone(crash_guard._read_state(path))

    def test_empty_path_returns_none(self):
        self.assertIsNone(crash_guard._read_state(""))
        self.assertIsNone(crash_guard._read_state(None))


class WriteFailureSilentTests(unittest.TestCase):
    def test_record_breadcrumb_swallows_write_error(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            crash_guard, "_write_state", side_effect=OSError("disk full")
        ):
            # 不得抛出。
            crash_guard.record_breadcrumb(base, "startup")

    def test_record_breadcrumb_resets_non_list_breadcrumbs(self):
        with tempfile.TemporaryDirectory() as base:
            path = crash_guard.state_path_for(base)
            state = crash_guard._fresh_state()
            state["breadcrumbs"] = "not-a-list"
            crash_guard._write_state(path, state)
            crash_guard.record_breadcrumb(base, "running")
            reread = crash_guard._read_state(path)
            self.assertIsInstance(reread["breadcrumbs"], list)
            self.assertEqual(reread["breadcrumbs"][-1]["stage"], "running")

    def test_mark_clean_exit_swallows_write_error(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            crash_guard, "_write_state", side_effect=OSError("disk full")
        ):
            crash_guard.mark_clean_exit(base)


# --- install_fault_handler 分支 ---------------------------------------------


class InstallFaultHandlerBranchTests(unittest.TestCase):
    def _release_handler(self):
        import faulthandler

        with contextlib.suppress(Exception):
            faulthandler.disable()
        handle = crash_guard._FAULT_HANDLER_FILE
        crash_guard._FAULT_HANDLER_FILE = None
        if handle:
            with contextlib.suppress(Exception):
                handle.close()

    def test_closes_previous_handle_before_reinstall(self):
        import shutil

        base = tempfile.mkdtemp()
        try:
            previous = mock.MagicMock()
            crash_guard._FAULT_HANDLER_FILE = previous
            self.assertTrue(crash_guard.install_fault_handler(base))
            previous.close.assert_called_once()
            self.assertIsNot(crash_guard._FAULT_HANDLER_FILE, previous)
        finally:
            self._release_handler()
            shutil.rmtree(base, ignore_errors=True)

    def test_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            crash_guard, "report_dir_for", side_effect=OSError("boom")
        ):
            self.assertFalse(crash_guard.install_fault_handler(base))


# --- launch_watchdog 分支 ----------------------------------------------------


class LaunchWatchdogBranchTests(unittest.TestCase):
    def _clear_watchdog_flag(self):
        # 递归守卫标志若存在会直接返回 False；这些用例需要它不存在。
        return os.environ.pop(crash_guard.WATCHDOG_ENV_FLAG, None)

    def test_success_path_spawns_detached_process(self):
        saved = self._clear_watchdog_flag()
        try:
            # FR-20260811-D17-2: argv[0] 使用真实存在的脚本路径（D17-2 兜底仅
            # 在 argv[0] 为 -c/-m/空/不存在时触发；此处锁定正常路径原样使用）。
            script = os.path.abspath(__file__)
            with tempfile.TemporaryDirectory() as base, mock.patch.object(
                crash_guard.subprocess, "Popen"
            ) as popen:
                self.assertTrue(crash_guard.launch_watchdog(base, argv=[script]))
                self.assertTrue(popen.called)
                command = popen.call_args[0][0]
                self.assertEqual(command[0], sys.executable)
                self.assertEqual(command[1], script)
                self.assertEqual(command[2], crash_guard.WATCHDOG_ARG)
                self.assertEqual(command[3], str(os.getpid()))
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    crash_guard._DETACHED_PROCESS | crash_guard._CREATE_NO_WINDOW,
                )
                self.assertEqual(
                    popen.call_args.kwargs["env"][crash_guard.WATCHDOG_ENV_FLAG], "1"
                )
                # 启动前应写入全新会话状态文件。
                state = crash_guard._read_state(crash_guard.state_path_for(base))
                self.assertIsInstance(state, dict)
                self.assertFalse(state["clean_exit"])
        finally:
            if saved is not None:
                os.environ[crash_guard.WATCHDOG_ENV_FLAG] = saved

    def test_frozen_branch_omits_script(self):
        saved = self._clear_watchdog_flag()
        try:
            with tempfile.TemporaryDirectory() as base, mock.patch.object(
                crash_guard.subprocess, "Popen"
            ) as popen, mock.patch.object(sys, "frozen", True, create=True):
                self.assertTrue(crash_guard.launch_watchdog(base, argv=["prog.py"]))
                command = popen.call_args[0][0]
                # 冻结包：executable 后直接是看门狗参数，不含脚本路径。
                self.assertEqual(command[0], sys.executable)
                self.assertEqual(command[1], crash_guard.WATCHDOG_ARG)
                self.assertNotIn("prog.py", command)
        finally:
            if saved is not None:
                os.environ[crash_guard.WATCHDOG_ENV_FLAG] = saved

    def test_empty_argv_falls_back_to_sys_argv(self):
        saved = self._clear_watchdog_flag()
        try:
            # FR-20260811-D17-2: sys.argv[0] 使用真实存在路径（兜底仅在
            # -c/-m/空/不存在时触发）。
            script = os.path.abspath(__file__)
            with tempfile.TemporaryDirectory() as base, mock.patch.object(
                crash_guard.subprocess, "Popen"
            ) as popen, mock.patch.object(sys, "argv", [script]):
                self.assertTrue(crash_guard.launch_watchdog(base, argv=[]))
                command = popen.call_args[0][0]
                self.assertEqual(command[1], script)
        finally:
            if saved is not None:
                os.environ[crash_guard.WATCHDOG_ENV_FLAG] = saved

    def test_relative_argv0_is_absolutized_for_watchdog_cwd(self):
        """FR-20260811-D17-2 边界：argv[0] 为当前 cwd 可解析的相对路径时，
        必须绝对化——Popen 以 cwd=base 启动，相对路径会在 base 下解析失败。"""
        saved = self._clear_watchdog_flag()
        try:
            with tempfile.TemporaryDirectory() as base, mock.patch.object(
                crash_guard.subprocess, "Popen"
            ) as popen, mock.patch.object(
                # 相对路径（tests/ 前缀）：在当前 cwd（仓库根）可解析。
                sys, "argv", [os.path.join("tests", os.path.basename(__file__))]
            ):
                self.assertTrue(crash_guard.launch_watchdog(base, argv=[]))
                command = popen.call_args[0][0]
                # 相对路径被绝对化为当前 cwd 下的绝对路径。
                self.assertEqual(command[1], os.path.abspath(__file__))
                self.assertTrue(os.path.isabs(command[1]))
        finally:
            if saved is not None:
                os.environ[crash_guard.WATCHDOG_ENV_FLAG] = saved

    def test_dash_c_argv_falls_back_to_main_file(self):
        """FR-20260811-D17-2: python -c 启动（argv[0]="-c"）时兜底 __main__.__file__。"""
        saved = self._clear_watchdog_flag()
        try:
            with tempfile.TemporaryDirectory() as base, mock.patch.object(
                crash_guard.subprocess, "Popen"
            ) as popen, mock.patch.object(
                sys, "argv", ["-c"]
            ), mock.patch.object(
                sys.modules["__main__"], "__file__", os.path.abspath(__file__),
                create=True,
            ):
                self.assertTrue(crash_guard.launch_watchdog(base, argv=[]))
                command = popen.call_args[0][0]
                self.assertEqual(command[1], os.path.abspath(__file__))
        finally:
            if saved is not None:
                os.environ[crash_guard.WATCHDOG_ENV_FLAG] = saved


# --- 崩溃报告格式化 / 写入 ----------------------------------------------------


class FormatReportNonDictCrumbTests(unittest.TestCase):
    def test_non_dict_crumbs_are_skipped(self):
        state = crash_guard._fresh_state()
        state["breadcrumbs"] = [
            "garbage",
            # 整数值用不与其他版本号/时间戳冲突的标记（原 42 在 v1.0.142 的
            # 版本字符串中出现导致断言误命中）。
            99991,
            {"stage": "running", "detail": "det", "at": "ts", "epoch": 1.0},
        ]
        text = crash_guard._format_crash_report(1, state, now=1_700_000_000.0)
        self.assertIn("running", text)
        self.assertIn("（det）", text)
        self.assertNotIn("garbage", text)
        self.assertNotIn("99991", text)

    def test_empty_breadcrumbs_show_placeholder(self):
        state = crash_guard._fresh_state()
        state["breadcrumbs"] = []
        text = crash_guard._format_crash_report(1, state, now=1_700_000_000.0)
        self.assertIn("（无面包屑）", text)


class WriteCrashReportBranchTests(unittest.TestCase):
    def test_makedirs_failure_returns_none(self):
        # 在已存在的普通文件名下创建目录会失败。
        with tempfile.TemporaryDirectory() as base:
            blocker = os.path.join(base, "file.txt")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            bad_dir = os.path.join(blocker, "crash_reports")
            self.assertIsNone(crash_guard._write_crash_report(bad_dir, 1, {}))

    def test_replace_failure_cleans_temp_and_returns_none(self):
        with tempfile.TemporaryDirectory() as report_dir:
            with mock.patch.object(crash_guard.os, "replace", side_effect=OSError("boom")):
                result = crash_guard._write_crash_report(report_dir, 1, {})
            self.assertIsNone(result)
            # finally 分支应清理临时文件，不留下 .crash_report.* 残留。
            leftovers = [
                name for name in os.listdir(report_dir) if name.startswith(".crash_report.")
            ]
            self.assertEqual(leftovers, [])


# --- _wait_for_parent / _get_process_creation_time ---------------------------


class _FakeKernel32:
    """可配置的 kernel32 替身，支持签名配置所需的属性赋值。"""

    def __init__(
        self,
        open_results=None,
        open_raises=False,
        wait_results=None,
        exit_code=0,
        get_exit_ok=True,
        creation_values=None,
        get_times_ok=True,
        close_raises=False,
    ):
        self._open_results = list(open_results if open_results is not None else [1234])
        self._open_raises = open_raises
        self._wait_results = list(wait_results if wait_results is not None else [0])
        self._exit_code = exit_code
        self._get_exit_ok = get_exit_ok
        self._creation_values = list(creation_values or [])
        self._get_times_ok = get_times_ok
        self._close_raises = close_raises
        self.open_calls = 0
        self.close_calls = 0

    def OpenProcess(self, access, inherit, pid):
        self.open_calls += 1
        if self._open_raises:
            raise OSError("open failed")
        if len(self._open_results) == 1:
            return self._open_results[0]
        return self._open_results.pop(0)

    def WaitForSingleObject(self, handle, timeout):
        if len(self._wait_results) == 1:
            return self._wait_results[0]
        return self._wait_results.pop(0)

    def GetExitCodeProcess(self, handle, code_ref):
        if not self._get_exit_ok:
            return 0
        ctypes.cast(code_ref, ctypes.POINTER(ctypes.c_ulong))[0] = self._exit_code
        return 1

    def CloseHandle(self, handle):
        self.close_calls += 1
        if self._close_raises:
            raise OSError("close failed")
        return 1

    def GetProcessTimes(self, handle, creation_ref, exit_ref, kernel_ref, user_ref):
        if not self._get_times_ok:
            return 0
        value = self._creation_values.pop(0) if self._creation_values else 111
        ctypes.cast(creation_ref, ctypes.POINTER(ctypes.c_uint64))[0] = value
        return 1


class GetProcessCreationTimeTests(unittest.TestCase):
    def test_success_returns_creation_value(self):
        fake = _FakeKernel32(creation_values=[4242])
        self.assertEqual(
            crash_guard._get_process_creation_time(fake, 1, ctypes), 4242
        )

    def test_failure_returns_none(self):
        fake = _FakeKernel32(get_times_ok=False)
        self.assertIsNone(crash_guard._get_process_creation_time(fake, 1, ctypes))


class WaitForParentBranchTests(unittest.TestCase):
    def test_access_denied_clean_exit_marker_returns_none(self):
        fake = _FakeKernel32(open_results=[0])
        with tempfile.TemporaryDirectory() as base:
            state_file = crash_guard.state_path_for(base)
            crash_guard._write_state(state_file, {"clean_exit": True})
            monotonic = mock.MagicMock(side_effect=[1000.0, 1000.0, 9999.0])
            with mock.patch.object(
                crash_guard.time, "monotonic", monotonic
            ), mock.patch.object(crash_guard.time, "sleep") as sleep_mock, mock.patch(
                "ctypes.get_last_error", return_value=5
            ):
                result = crash_guard._wait_for_parent(4242, kernel32=fake, state_file=state_file)
        self.assertIsNone(result)
        sleep_mock.assert_called_once_with(crash_guard._CLEAN_EXIT_SETTLE_SECONDS)

    def test_access_denied_then_openable_falls_into_wait(self):
        # 前两次 OpenProcess 失败（ACCESS_DENIED），第三次成功 → 进入正常等待。
        fake = _FakeKernel32(open_results=[0, 0, 1234], exit_code=9)
        with tempfile.TemporaryDirectory() as base:
            state_file = crash_guard.state_path_for(base)
            crash_guard._write_state(state_file, {"clean_exit": False})
            # monotonic: 1) deadline=1000+30  2) loop check 1000<1030 → iter1
            # 3) loop check 1000<1030 → iter2 → OpenProcess succeeds → break
            monotonic = mock.MagicMock(side_effect=[1000.0, 1000.0, 1000.0])
            with mock.patch.object(
                crash_guard.time, "monotonic", monotonic
            ), mock.patch.object(crash_guard.time, "sleep"), mock.patch(
                "ctypes.get_last_error", return_value=5
            ):
                result = crash_guard._wait_for_parent(4242, kernel32=fake, state_file=state_file)
        self.assertEqual(result, 9)
        self.assertEqual(fake.open_calls, 3)

    def test_access_denied_timeout_returns_none(self):
        fake = _FakeKernel32(open_results=[0])
        # 首次检查即超过截止时间 → 轮询循环不进入，直接返回 None。
        monotonic = mock.MagicMock(side_effect=[1000.0, 99999.0])
        with mock.patch.object(crash_guard.time, "monotonic", monotonic), mock.patch.object(
            crash_guard.time, "sleep"
        ) as sleep_mock, mock.patch(
            "ctypes.get_last_error", return_value=5
        ):
            result = crash_guard._wait_for_parent(4242, kernel32=fake)
        self.assertIsNone(result)
        sleep_mock.assert_not_called()

    def test_get_last_error_failure_treated_as_gone(self):
        fake = _FakeKernel32(open_results=[0])
        with mock.patch(
            "ctypes.get_last_error", side_effect=RuntimeError("nope")
        ):
            result = crash_guard._wait_for_parent(4242, kernel32=fake)
        self.assertIsNone(result)

    def test_pid_reuse_detected_returns_none(self):
        # 首次等待超时，创建时间发生变化 → 判定 PID 复用，返回 None。
        fake = _FakeKernel32(wait_results=[258, 0], creation_values=[100, 200])
        result = crash_guard._wait_for_parent(4242, kernel32=fake)
        self.assertIsNone(result)
        self.assertEqual(fake.close_calls, 1)

    def test_timeout_then_exit_returns_exit_code(self):
        # 首次超时但创建时间未变 → 继续等待，第二次退出 → 返回退出码。
        fake = _FakeKernel32(wait_results=[258, 0], exit_code=5, creation_values=[100, 100])
        self.assertEqual(crash_guard._wait_for_parent(4242, kernel32=fake), 5)

    def test_get_exit_code_failure_returns_none(self):
        fake = _FakeKernel32(wait_results=[0], get_exit_ok=False)
        self.assertIsNone(crash_guard._wait_for_parent(4242, kernel32=fake))
        self.assertEqual(fake.close_calls, 1)

    def test_close_handle_failure_still_returns_exit_code(self):
        fake = _FakeKernel32(wait_results=[0], exit_code=3, close_raises=True)
        self.assertEqual(crash_guard._wait_for_parent(4242, kernel32=fake), 3)

    def test_top_level_exception_returns_none(self):
        fake = _FakeKernel32(open_raises=True)
        self.assertIsNone(crash_guard._wait_for_parent(4242, kernel32=fake))


# --- _watchdog_run / watchdog_main 分支 --------------------------------------


def _write_state_file(report_dir, **overrides):
    state = crash_guard._fresh_state()
    state.update(overrides)
    state_file = os.path.join(report_dir, "session_state.json")
    crash_guard._write_state(state_file, state)
    return state_file


class WatchdogRunBranchTests(unittest.TestCase):
    def _argv(self, report_dir, state_file):
        return [
            "prog",
            crash_guard.WATCHDOG_ARG,
            str(os.getpid()),
            state_file,
            report_dir,
        ]

    def test_wait_func_exception_treated_as_crash(self):
        with tempfile.TemporaryDirectory() as report_dir:
            state_file = _write_state_file(report_dir, clean_exit=False, last_stage="running")

            def boom(_pid):
                raise RuntimeError("wait failed")

            rc = crash_guard._watchdog_run(
                self._argv(report_dir, state_file),
                sleep_func=lambda _s: None,
                wait_func=boom,
            )
            self.assertEqual(rc, 0)
            self.assertTrue(
                [name for name in os.listdir(report_dir) if name.startswith("crash_")]
            )

    def test_sleep_interrupted_still_proceeds(self):
        with tempfile.TemporaryDirectory() as report_dir:
            state_file = _write_state_file(report_dir, clean_exit=True)

            def bad_sleep(_s):
                raise InterruptedError("interrupted")

            rc = crash_guard._watchdog_run(
                self._argv(report_dir, state_file),
                sleep_func=bad_sleep,
                wait_func=lambda _pid: 0,
            )
            # clean_exit 标记仍在 → 不写报告，正常返回。
            self.assertEqual(rc, 0)
            self.assertFalse(
                [name for name in os.listdir(report_dir) if name.startswith("crash_")]
            )

    def test_writer_exception_is_swallowed(self):
        with tempfile.TemporaryDirectory() as report_dir:
            state_file = _write_state_file(report_dir, clean_exit=False)

            def bad_writer(_dir, _code, _state):
                raise OSError("write failed")

            rc = crash_guard._watchdog_run(
                self._argv(report_dir, state_file),
                sleep_func=lambda _s: None,
                wait_func=lambda _pid: 1,
                writer=bad_writer,
            )
            self.assertEqual(rc, 0)

    def test_default_wait_func_branch_with_dead_parent(self):
        # 不传 wait_func → 走默认 _wait_for_parent；用一个已不存在的 PID，
        # OpenProcess 失败后立即返回 None，不会真实等待。
        with tempfile.TemporaryDirectory() as report_dir:
            state_file = _write_state_file(report_dir, clean_exit=False, last_stage="boot")
            with mock.patch.object(
                crash_guard, "_wait_for_parent", return_value=None
            ) as wait_mock:
                rc = crash_guard._watchdog_run(
                    self._argv(report_dir, state_file), sleep_func=lambda _s: None
                )
            self.assertEqual(rc, 0)
            wait_mock.assert_called_once()
            self.assertTrue(
                [name for name in os.listdir(report_dir) if name.startswith("crash_")]
            )


class WatchdogMainExceptionTests(unittest.TestCase):
    def test_internal_exception_returns_zero(self):
        with mock.patch.object(
            crash_guard, "_watchdog_run", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(crash_guard.watchdog_main(["prog"]), 0)


if __name__ == "__main__":
    unittest.main()
