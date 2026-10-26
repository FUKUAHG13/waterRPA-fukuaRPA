# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Lifecycle owner for the global mouse/keyboard recorder overlay."""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

from ..input_tools import RecorderUI

if TYPE_CHECKING:
    from .host_view import HostView


class RecordingController:
    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.recorder = None

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("录制器所属窗口已销毁")
        return host

    def start(self):
        self.close()
        host = self.host
        host.showMinimized()
        recorder = RecorderUI(
            tasks_callback=self._add_tasks,
            log_callback=host.append_log,
            restore_callback=host.showNormal,
        )
        self.recorder = recorder
        recorder.destroyed.connect(
            lambda *_args, current=recorder: self._clear_if_current(current)
        )
        return recorder

    def _add_tasks(self, tasks):
        host = self.host
        # FR-901-002: add_rows 在运行期编辑锁开启时返回 0（静默丢弃）。把返回值
        # 交给 RecorderUI.on_recorded，使"已生成 N 步"的完成提示与实际插入结果一致。
        return host.add_rows(tasks)

    def _clear_if_current(self, recorder):
        if self.recorder is recorder:
            self.recorder = None

    def close(self):
        recorder = self.recorder
        if recorder is None:
            return
        try:
            recorder.close()
        except RuntimeError:
            self.recorder = None
