# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Deferred initialization of heavy runtime backends."""

from __future__ import annotations

import contextlib
import threading
import time
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from ...log_policy import (
    LOG_BACKEND,
)
from ...logging_service import write_log
from ...privacy import private_error_text, safe_error_name

if TYPE_CHECKING:
    from .host_view import HostView


class RuntimeInitBridge(QObject):
    completed = Signal(bool, str, float)


class RuntimeInitController:
    def __init__(self, host: HostView, *, ready):
        self._host_ref = weakref.ref(host)
        self.ready = bool(ready)
        self.in_progress = False
        self.thread = None
        self.bridge = RuntimeInitBridge(host)
        self.bridge.completed.connect(self.on_initialized)

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("运行库初始化窗口已销毁")
        return host

    def initialize_async(self):
        if self.ready or self.in_progress:
            return
        self.in_progress = True
        started = time.perf_counter()

        def initialize():
            try:
                self.host.engine.initialize_backends()
                result = (True, "")
            except Exception as error:
                result = (False, str(error))
            with contextlib.suppress(RuntimeError):
                self.bridge.completed.emit(
                    result[0],
                    result[1],
                    (time.perf_counter() - started) * 1000.0,
                )

        self.thread = threading.Thread(
            target=initialize,
            daemon=True,
            name="fukuaRPA-runtime-init",
        )
        self.thread.start()

    def on_initialized(self, ok, error, elapsed_ms):
        host = self.host
        self.in_progress = False
        self.ready = bool(ok)
        if ok:
            if not host.run_controller.is_active:
                host.start_btn.setEnabled(True)
                host.single_step_btn.setEnabled(True)
            host.start_btn.setToolTip("")
            write_log(f"运行库后台初始化完成，用时 {elapsed_ms:.1f} ms。",
                         category=LOG_BACKEND,
                     )
            return
        host.start_btn.setEnabled(False)
        host.single_step_btn.setEnabled(False)
        host.start_btn.setToolTip("识别运行库初始化失败")
        write_log(
            f"运行库后台初始化失败：{safe_error_name(error)}",
            private_message=f"运行库后台初始化失败：{private_error_text(error)}",
                     category=LOG_BACKEND,
                     critical=True,
                 )
