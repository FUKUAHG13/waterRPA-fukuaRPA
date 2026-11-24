# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Lifecycle owner for the global mouse/keyboard recorder overlay."""

from __future__ import annotations

import copy
import weakref
from typing import TYPE_CHECKING

from ...workflow_metadata import new_editor_group
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

    def start(self, _checked=False, *, mode=None):
        """Open the raw-playback recorder overlay.

        DESIGN（v1.1.19）：所有用户可达的新录制入口统一走原样回放——主窗口
        点击录制不再弹模式选择窗口，也不再提供「生成操作步骤」入口。结果在
        主列表生成一组可编辑的原速步骤（见 ``_add_tasks``）。``mode`` 仅保留
        给程序化/测试入口；无论是 ``None`` 还是旧值 ``"steps"`` 都归一到 raw，
        避免遗留生成操作步骤的路径。``_checked`` 吸收 ``clicked(bool)`` 参数。
        """
        self.close()
        host = self.host
        # 忽略外部传入的 mode：新录制一律 raw。保留关键字仅供程序化/测试兼容。
        host.showMinimized()
        recorder = RecorderUI(
            tasks_callback=self._add_tasks,
            log_callback=host.append_log,
            restore_callback=host.showNormal,
            mode="raw",
        )
        self.recorder = recorder
        recorder.destroyed.connect(
            lambda *_args, current=recorder: self._clear_if_current(current)
        )
        return recorder

    def _add_tasks(self, tasks):
        host = self.host
        # 每次成功录制独立成组：本批全部步骤共享同一个新组 ID，作为一级组
        # 追加到工作流末尾。组元数据随步骤进入 add_rows 的同一次提交与撤销快照。
        # 空录制不建组；实际插入数由 add_rows 决定，运行锁拒绝时返回 0，
        # 组内步骤未进入方案，方案与撤销历史保持录制前状态。
        batch = [task for task in (tasks or ()) if isinstance(task, dict)]
        if batch:
            # R2（用户 2026-11-22 裁定）：原样回放批次在组名上标注「原速回放」，
            # 与倍速设置说明一致。标注只影响编辑器展示，不改变步骤执行。
            is_raw = any(str(task.get("fidelity", "")) == "raw" for task in batch)
            group_name = "录制步骤（原速回放）" if is_raw else "录制步骤"
            group = new_editor_group(group_name)
            for task in batch:
                # INVARIANT: 一个组 ID 只标识本批这一个连续区间；深拷贝避免
                # 步骤间共享同一 dict 导致后续编辑相互串改。
                task["editor_groups"] = [copy.deepcopy(group)]
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
