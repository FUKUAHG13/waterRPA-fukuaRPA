"""fukuaRPA application entry point.

Implementation lives in the ``fukua_rpa`` package. This file intentionally keeps
only process setup, a small compatibility export surface, and application startup.
"""

import atexit
import contextlib
import ctypes
import importlib
import json
import multiprocessing
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

_PYTHON_START_TIME = time.perf_counter()

# INVARIANT: establish Per-Monitor V2 before importing PySide6. Low-level mouse
# hooks, native screenshots and input injection then share physical desktop
# pixels, including negative coordinates on mixed-DPI secondary displays.
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
from fukua_rpa.dpi_awareness import enable_per_monitor_v2  # noqa: E402

_PER_MONITOR_DPI_READY = enable_per_monitor_v2()

from PySide6.QtCore import (  # noqa: E402
    QLibraryInfo,
    QObject,
    QTimer,
    QTranslator,
    Signal,
)
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fukua_rpa.constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    NATIVE_CORE_MIN_VERSION,
    PRODUCT_NAME,
    SUPPORTED_WINDOWS_TEXT,
)
from fukua_rpa.paths import get_base_dir, get_resource_path  # noqa: E402
from fukua_rpa.session import SingleInstanceGuard  # noqa: E402

if TYPE_CHECKING:
    from fukua_rpa.engine import RPAEngine
    from fukua_rpa.ui.input_tools import RecorderUI
    from fukua_rpa.ui.main_window import RPAWindow
    from fukua_rpa.vision.core import (
        NativeVisionCore,
        build_scale_values,
        template_detail_status,
    )

__all__ = [
    "APP_VERSION",
    "BUILD_NAME",
    "NATIVE_CORE_MIN_VERSION",
    "NativeVisionCore",
    "PRODUCT_NAME",
    "RPAEngine",
    "RPAWindow",
    "RecorderUI",
    "SUPPORTED_WINDOWS_TEXT",
    "build_scale_values",
    "template_detail_status",
]

_LAZY_EXPORTS = {
    "NativeVisionCore": ("fukua_rpa.vision.core", "NativeVisionCore"),
    "RPAEngine": ("fukua_rpa.engine", "RPAEngine"),
    "RPAWindow": ("fukua_rpa.ui.main_window", "RPAWindow"),
    "RecorderUI": ("fukua_rpa.ui.input_tools", "RecorderUI"),
    "build_scale_values": ("fukua_rpa.vision.core", "build_scale_values"),
    "template_detail_status": ("fukua_rpa.vision.core", "template_detail_status"),
}


def __getattr__(name):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def _emergency_cleanup():
    """SAFETY: 异常退出时的最后防线——清理 Hook、关闭故障处理器日志。

    正常退出路径有各自的显式清理，此处仅兜底。
    Runner 子进程由 Windows Job Object (KILL_ON_JOB_CLOSE) 保证终止，
    无需在此重复处理。
    """
    try:
        from fukua_rpa.hook_backend import uninstall_all_hooks

        uninstall_all_hooks()
    except Exception:
        pass
    try:
        import fukua_rpa.crash_guard as _cg

        handle = getattr(_cg, "_FAULT_HANDLER_FILE", None)
        if handle is not None:
            handle.close()
            _cg._FAULT_HANDLER_FILE = None
    except Exception:
        pass


atexit.register(_emergency_cleanup)


class StartupLoadBridge(QObject):
    loaded = Signal(object, float)
    failed = Signal(str)


def install_qt_chinese_translator(app):
    """Translate standard Qt context menus and dialogs without custom forks."""

    translator = QTranslator(app)
    candidates = [
        os.path.join(
            QLibraryInfo.path(QLibraryInfo.TranslationsPath),
            "qtbase_zh_CN.qm",
        ),
        get_resource_path(
            os.path.join("PySide6", "translations", "qtbase_zh_CN.qm")
        ),
    ]
    for path in candidates:
        if os.path.isfile(path) and translator.load(path):
            app.installTranslator(translator)
            app._fukua_qt_translator = translator
            return True
    return False


def _atomic_write_text(target, text):
    # FR-20260812 建议-架构-3: 复用 io_utils.atomic_write_text，消除重复实现。
    # 惰性导入避免冷启动路径引入额外模块。
    from fukua_rpa.io_utils import atomic_write_text

    atomic_write_text(target, text)


def main():
    multiprocessing.freeze_support()
    if "--crash-watchdog" in sys.argv:
        # Watchdog child process: wait for the parent and report hard crashes.
        # Routed first so it never touches the single-instance guard or GUI.
        from fukua_rpa.crash_guard import watchdog_main

        return watchdog_main(sys.argv)
    if "--runner-child" in sys.argv:
        from fukua_rpa.runner.process import main as runner_main

        index = sys.argv.index("--runner-child")
        child_args = sys.argv[index + 1 :]
        if not child_args:
            return 2
        return runner_main(child_args)

    native_smoke_target = None
    native_smoke_requested = "--native-smoke" in sys.argv
    if "--native-smoke-file" in sys.argv:
        native_smoke_requested = True
        index = sys.argv.index("--native-smoke-file")
        if index + 1 >= len(sys.argv):
            return 2
        native_smoke_target = os.path.abspath(sys.argv[index + 1])
    if native_smoke_requested:
        from fukua_rpa.native_smoke import run_native_smoke

        report = run_native_smoke(get_base_dir())
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if native_smoke_target:
            _atomic_write_text(native_smoke_target, serialized)
        else:
            print(serialized)
        return 0 if report.get("ok") else 1

    uia_smoke_target = None
    uia_smoke_requested = "--uia-smoke" in sys.argv
    if "--uia-smoke-file" in sys.argv:
        uia_smoke_requested = True
        index = sys.argv.index("--uia-smoke-file")
        if index + 1 >= len(sys.argv):
            return 2
        uia_smoke_target = os.path.abspath(sys.argv[index + 1])
    if uia_smoke_requested:
        from fukua_rpa.uia_smoke import run_uia_smoke

        report = run_uia_smoke()
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if uia_smoke_target:
            _atomic_write_text(uia_smoke_target, serialized)
        else:
            print(serialized)
        return 0 if report.get("ok") else 1

    self_test_target = None
    self_test_requested = "--self-test" in sys.argv
    if "--self-test-file" in sys.argv:
        self_test_requested = True
        index = sys.argv.index("--self-test-file")
        if index + 1 >= len(sys.argv):
            return 2
        self_test_target = os.path.abspath(sys.argv[index + 1])
    if self_test_requested:
        from fukua_rpa.diagnostics import run_runtime_diagnostics

        report = run_runtime_diagnostics()
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if self_test_target:
            _atomic_write_text(self_test_target, serialized)
        else:
            print(serialized)
        return 0 if report["ok"] else 1

    startup_smoke_target = None
    # FR-101-001: get_base_dir()/runtime_path_info() 属早期启动调用（与下方 makedirs、
    # SingleInstanceGuard 同类），portable.flag+只读目录或 %LOCALAPPDATA% 创建失败时
    # 会抛异常；若不兜底，冻结包（console=False）会静默退出且无任何提示或崩溃报告。
    # 这里与 FR-20260812-D17-003 的既有分支保持一致的可见错误语义。
    try:
        startup_runtime_dir = get_base_dir()
    except Exception as exc:  # noqa: BLE001 - 早期启动兜底，任何异常都应可见
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                f"fukuaRPA 无法确定数据目录：\n{exc}\n\n"
                "请检查解压目录是否完整、磁盘与目录权限是否正常后重试。",
                "fukuaRPA 启动失败",
                0x00000010,
            )
        return 4
    if "--startup-smoke-file" in sys.argv:
        index = sys.argv.index("--startup-smoke-file")
        if index + 1 >= len(sys.argv):
            return 2
        startup_smoke_target = os.path.abspath(sys.argv[index + 1])
    if "--startup-runtime-dir" in sys.argv:
        index = sys.argv.index("--startup-runtime-dir")
        if index + 1 >= len(sys.argv):
            return 2
        startup_runtime_dir = os.path.abspath(sys.argv[index + 1])
    # 首次启动选择：必须在任何新配置写入之前判断“是否已有既有配置”，否则会把
    # 老用户误判成全新安装并再次打断。此处只做存在性判断，不读取、不创建配置。
    existing_config_before_start = os.path.exists(
        os.path.join(startup_runtime_dir, "config.ini")
    )
    # FR-20260812-D17-003: 看门狗安装前的早期启动调用（创建数据目录、单实例
    # 守卫）没有兜底时，冻结包（console=False）会在启动失败时静默退出且无任何
    # 提示或报告。这里补 try/except：任何早期异常都给出可见错误并返回非零码。
    try:
        os.makedirs(startup_runtime_dir, exist_ok=True)
    except OSError as exc:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                f"fukuaRPA 无法创建数据目录：\n{exc}\n\n"
                "请检查磁盘空间或目录权限后重试。",
                "fukuaRPA 启动失败",
                0x00000010,
            )
        return 4

    try:
        instance_guard = SingleInstanceGuard(startup_runtime_dir)
    except Exception as exc:  # noqa: BLE001 - 早期启动兜底，任何异常都应可见
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                f"fukuaRPA 启动失败：\n{exc}\n\n"
                "请检查解压目录是否完整、权限是否正常后重试。",
                "fukuaRPA 启动失败",
                0x00000010,
            )
        return 4
    if not instance_guard.acquire():
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                "当前解压目录中的 fukuaRPA 已经在运行。\n\n"
                "如任务栏中没有窗口，请先在任务管理器中结束旧进程后再启动。",
                "fukuaRPA 已在运行",
                0x00000040,
            )
        return 3
    # FR-20261012-726: 与上面的 makedirs / SingleInstanceGuard 同构，看门狗安装
    # 段也必须有可见兜底。它原先在主 try 之外，冻结包（console=False）下异常会
    # 静默退出，既无提示也无崩溃报告。breadcrumb 仍在成功安装之后才写。
    try:
        # Lazy import keeps crash guarding out of the cold-start import path.
        from fukua_rpa import crash_guard

        crash_guard.install_fault_handler(startup_runtime_dir)
        crash_guard.launch_watchdog(startup_runtime_dir)
        crash_guard.record_breadcrumb(startup_runtime_dir, "startup")
    except Exception as exc:  # noqa: BLE001 - 早期启动兜底，任何异常都应可见
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                f"fukuaRPA 启动失败（崩溃看门狗初始化异常）：\n{exc}\n\n"
                "请检查解压目录是否完整、权限是否正常后重试。",
                "fukuaRPA 启动失败",
                0x00000010,
            )
        return 4
    with contextlib.suppress(Exception):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FUKUA.fukuaRPA.v1")
    _normal_exit = True
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        install_qt_chinese_translator(app)
        app.setApplicationName(PRODUCT_NAME)
        app.setApplicationDisplayName(f"{PRODUCT_NAME} {APP_VERSION}")
        icon = QIcon(get_resource_path(os.path.join("assets", "fukuaRPA.ico")))
        if not icon.isNull():
            app.setWindowIcon(icon)
        from fukua_rpa.ui.startup import StartupShell

        shell = StartupShell()
        if not icon.isNull():
            shell.setWindowIcon(icon)
        shell.show()
        crash_guard.record_breadcrumb(startup_runtime_dir, "shell_shown")
        app._fukua_startup_shell = shell
        app._fukua_main_window = None
        app._fukua_startup_cancelled = False
        bridge = StartupLoadBridge(app)
        app._fukua_startup_bridge = bridge
        startup_started = time.perf_counter()
        startup_metrics = {
            "python_to_shell_first_paint_ms": None,
            "workspace_import_ms": None,
            "workspace_visible_ms": None,
            "runtime_ready_ms": None,
            "runtime_backend_ms": None,
        }
        app._fukua_startup_metrics = startup_metrics

        def write_startup_smoke(ok, error=""):
            if not startup_smoke_target:
                return
            report = {
                "format": "fukuaRPA_startup_smoke",
                "application_version": APP_VERSION,
                "build_name": BUILD_NAME,
                "ok": bool(ok),
                **startup_metrics,
                "error": str(error or ""),
            }
            _atomic_write_text(
                startup_smoke_target,
                json.dumps(report, ensure_ascii=False, indent=2),
            )

        def cancel_startup():
            app._fukua_startup_cancelled = True
            app.quit()

        shell.cancelled.connect(cancel_startup)

        def workspace_loaded(window_class, import_ms):
            if app._fukua_startup_cancelled:
                return
            try:
                window = window_class(
                    base_dir=startup_runtime_dir, defer_runtime=True
                )
                if not icon.isNull():
                    window.setWindowIcon(icon)
                app._fukua_main_window = window
                window.show()
                crash_guard.record_breadcrumb(startup_runtime_dir, "workspace_loaded")
                startup_metrics["workspace_import_ms"] = round(import_ms, 3)
                startup_metrics["workspace_visible_ms"] = round(
                    (time.perf_counter() - _PYTHON_START_TIME) * 1000.0, 3
                )

                def runtime_initialized(ok, error, backend_ms):
                    startup_metrics["runtime_backend_ms"] = round(
                        float(backend_ms), 3
                    )
                    startup_metrics["runtime_ready_ms"] = round(
                        (time.perf_counter() - _PYTHON_START_TIME) * 1000.0, 3
                    )
                    crash_guard.record_breadcrumb(startup_runtime_dir, "runtime_ready")
                    write_startup_smoke(ok, error)
                    if startup_smoke_target:
                        QTimer.singleShot(50, app.quit)

                window.runtime_init_bridge.completed.connect(runtime_initialized)
                shell.handoff()
                # 首次启动选择只在交互式 GUI 出现；自检、烟雾测试、Runner 与
                # 后台辅助进程不弹窗（烟雾测试随后会 app.quit）。
                if not startup_smoke_target:
                    window.schedule_first_launch_choice(
                        existing_config=existing_config_before_start
                    )
                from fukua_rpa.logging_service import write_log

                write_log(
                    f"启动首帧后加载：工作区导入 {import_ms:.1f} ms，"
                    f"主窗口可见共 {(time.perf_counter() - startup_started) * 1000.0:.1f} ms。"
                )
            except Exception as error:
                workspace_failed(str(error))

        def workspace_failed(message):
            if app._fukua_startup_cancelled:
                return
            shell.status_label.setText("工作区加载失败")
            write_startup_smoke(False, message)
            if startup_smoke_target:
                QTimer.singleShot(0, lambda: app.exit(1))
                return
            QMessageBox.critical(shell, "启动失败", message)
            app.exit(1)

        bridge.loaded.connect(workspace_loaded)
        bridge.failed.connect(workspace_failed)

        def load_workspace():
            started = time.perf_counter()
            try:
                from fukua_rpa.ui.main_window import RPAWindow

                bridge.loaded.emit(
                    RPAWindow, (time.perf_counter() - started) * 1000.0
                )
            except Exception as error:
                bridge.failed.emit(str(error))

        def begin_workspace_load():
            loader = threading.Thread(
                target=load_workspace,
                daemon=True,
                name="fukuaRPA-workspace-loader",
            )
            app._fukua_workspace_loader = loader
            loader.start()

        _workspace_load_started = False

        def shell_first_painted(painted_at):
            nonlocal _workspace_load_started
            # FR-20260812-D16-001: 500ms fallback 触发后晚到的 paintEvent 不得
            # 覆写已写入的 -1.0 哨兵。只有工作区加载尚未开始时 paint 才记录
            # 首帧耗时并触发加载。
            if _workspace_load_started:
                return
            _workspace_load_started = True
            startup_metrics["python_to_shell_first_paint_ms"] = round(
                (float(painted_at) - _PYTHON_START_TIME) * 1000.0, 3
            )
            QTimer.singleShot(0, begin_workspace_load)

        def _fallback_workspace_load():
            nonlocal _workspace_load_started
            if not _workspace_load_started:
                _workspace_load_started = True
                startup_metrics["python_to_shell_first_paint_ms"] = -1.0
                begin_workspace_load()

        shell.first_painted.connect(shell_first_painted)
        # SAFETY: Frozen packages on Windows 11 may never receive paintEvent
        # (DWM composition skips QMainWindow paint when central widget is opaque).
        # Fallback ensures workspace load starts even without a paint event.
        QTimer.singleShot(500, _fallback_workspace_load)
        crash_guard.record_breadcrumb(startup_runtime_dir, "running")
        return app.exec()
    except (SystemExit, KeyboardInterrupt):
        # FR-WF-001: 用户主动退出 / 中断不视为崩溃，按正常路径处理（仍写 clean_exit）。
        raise
    except BaseException as exc:  # noqa: BLE001 - GUI 段兜底，未捕获异常须可见并生成崩溃报告
        _normal_exit = False
        with contextlib.suppress(Exception):
            from fukua_rpa.logging_service import write_log

            write_log(f"GUI 启动段未捕获异常：{exc!r}")
        import logging as _logging

        _logging.getLogger("fukuaRPA").exception("GUI 启动段未捕获异常")
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                0,
                f"fukuaRPA 启动失败：\n{exc}\n\n"
                "程序遇到未处理的错误，已记录崩溃报告。",
                "fukuaRPA 启动失败",
                0x00000010,
            )
        return 1
    finally:
        from fukua_rpa.logging_service import flush_logs

        flush_logs()
        if _normal_exit:
            with contextlib.suppress(Exception):
                crash_guard.mark_clean_exit(startup_runtime_dir)
        instance_guard.release()


if __name__ == "__main__":
    sys.exit(main())
