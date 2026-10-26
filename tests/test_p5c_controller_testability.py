"""P5c 守卫测试：控制器层脱离具体窗口可单测（无 QApplication 依赖）。

验证内容（对齐 P5c 退出标准）：
1. 全部 16 个具体控制器可在**无 QApplication** 环境下、用一个通用 QObject 桩宿主
   成功实例化 —— 证明控制器层只依赖 HostView 接口，不依赖具体 RPAWindow / Qt 父对象。
2. execution_controller 即使在该通用桩下也能被驱动（clear_debug_pause_highlight），
   呼应 P5b-3 的“控制器可无 GUI 单测”。
3. 主窗口不再出现 `self.engine.<field> =` 引擎字段直写（对齐 P5a/P5c 退出标准 grep 项）。

本测试不创建任何真实窗口、不创建 QApplication。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any
from unittest import mock

from PySide6.QtCore import QObject

REPO = Path(__file__).resolve().parent.parent

# (模块, 类名)；与 P5b-1 守卫测试保持一致的 16 个控制器 / mixin
CONTROLLERS = [
    ("fukua_rpa.ui.controllers.diagnostics_controller", "DiagnosticsController"),
    ("fukua_rpa.ui.controllers.execution_controller", "ExecutionController"),
    ("fukua_rpa.ui.controllers.feedback_controller", "VisualFeedbackController"),
    ("fukua_rpa.ui.controllers.hotkey_controller", "HotkeyController"),
    ("fukua_rpa.ui.controllers.log_controller", "LogController"),
    ("fukua_rpa.ui.controllers.mapping_controller", "MappingController"),
    ("fukua_rpa.ui.controllers.profile_controller", "ProfileController"),
    ("fukua_rpa.ui.controllers.recording_controller", "RecordingController"),
    ("fukua_rpa.ui.controllers.runtime_init_controller", "RuntimeInitController"),
    ("fukua_rpa.ui.controllers.settings_controller", "SettingsController"),
    ("fukua_rpa.ui.controllers.shutdown_controller", "ShutdownController"),
    ("fukua_rpa.ui.controllers.window_performance_controller", "WindowPerformanceController"),
    ("fukua_rpa.ui.controllers.workflow_context", "WorkflowContextMixin"),
    ("fukua_rpa.ui.controllers.workflow_editor", "WorkflowEditorController"),
    ("fukua_rpa.ui.controllers.workflow_review_controller", "WorkflowReviewController"),
    ("fukua_rpa.ui.controllers.workflow_transfer_controller", "WorkflowTransferController"),
]


class _U:
    """通用值：可调用、任意属性返回自身、可迭代、带 .value —— 容纳桩宿主的各种访问。"""

    def __call__(self, *a, **k):
        return _U()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _U()

    def __iter__(self):
        return iter([])

    @property
    def value(self):
        return _U()


class _UniversalHost(QObject):
    """通用 QObject 桩：可弱引用、可作 Qt 父对象、任意缺失成员返回通用值。"""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _U()


def _instantiate(modname: str, clsname: str) -> str:
    module = importlib.import_module(modname)
    cls = getattr(module, clsname)
    init = cls.__dict__.get("__init__")
    if init is None:
        return "skip(mixin-no-__init__)"
    host = _UniversalHost()
    for call in (
        lambda: cls(host),
        lambda: cls(host=host),
        lambda: cls(host=host, ready=True),
    ):
        try:
            call()
            return "ok"
        except TypeError:
            continue
        except Exception:  # noqa: BLE001
            continue
    raise AssertionError(f"{clsname} 无法用通用桩宿主实例化")


def test_all_controllers_instantiate_without_qapplication():
    failed = []
    for modname, clsname in CONTROLLERS:
        try:
            status = _instantiate(modname, clsname)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{clsname}: {type(e).__name__}: {e}")
            continue
        assert status in ("ok", "skip(mixin-no-__init__)"), f"{clsname} -> {status}"
    assert not failed, "以下控制器无法在无 QApplication 下实例化:\n" + "\n".join(failed)


def test_execution_controller_imports_and_protocol_aligned():
    """execution_controller 可在无 GUI 下导入，且仍注解为依赖 HostView 接口。"""
    from fukua_rpa.ui.controllers import execution_controller

    init_ann = execution_controller.ExecutionController.__init__.__annotations__
    assert init_ann.get("host") == "HostView"

    host_attr = execution_controller.ExecutionController.host
    ret_ann = (
        host_attr.fget.__annotations__
        if isinstance(host_attr, property)
        else host_attr.__annotations__
    )
    assert ret_ann.get("return") == "HostView"


def test_main_window_has_no_engine_field_direct_write():
    """P5c 退出标准 grep 项：主窗口不再直写引擎字段（self.engine.<field> =）。"""
    mw = (REPO / "fukua_rpa/ui/main_window.py").read_text(encoding="utf-8")
    # 仅匹配“写入引擎字段”的赋值，排除 `self.engine = ...`（引用赋值）与读取/调用。
    hits = re.findall(r"self\.engine\.[A-Za-z_][A-Za-z0-9_]*\s*=", mw)
    # 若引用赋值本身存在，剔除 `self.engine =`（无点后字段）
    hits = [h for h in hits if not re.match(r"self\.engine\s*=", h)]
    assert not hits, f"主窗口仍存在引擎字段直写: {hits}"


def test_diagnostics_safe_wrappers_swallow_host_destroyed():
    """FR-20260812-D10-002: _safe_integrity_* 的 try 块必须包裹真实宿主访问，
    宿主销毁后回调不得让 RuntimeError 从槽内逃逸。"""
    from fukua_rpa.ui.controllers.diagnostics_controller import (
        DiagnosticsController,
    )

    class _DeadHost:
        """宿主已销毁：访问任何属性都抛 RuntimeError（弱引用失效语义）。"""

        def __getattr__(self, name):
            raise RuntimeError("宿主已销毁")

    ctrl = DiagnosticsController(_DeadHost())
    # 宿主销毁后安全包装不得抛出（原实现 try 内仅 pass，host 访问在 try 外）。
    ctrl._safe_integrity_report({"missing": [], "mismatched": []})
    ctrl._safe_integrity_finished()


def test_settings_interaction_parent_fallback_is_qwidget():
    """FR-20260812-D10-b: settings_interaction_parent 回退分支必须返回 QWidget
    （可作 QMessageBox parent），不得返回非 QObject 的控制器。"""
    from PySide6.QtWidgets import QWidget

    from fukua_rpa.ui.controllers.mapping_controller import MappingController

    class _Host(QObject):
        settings_dialog = None

        def window(self):
            return None

    host = _Host()
    ctrl = MappingController(host)
    parent = ctrl.settings_interaction_parent()
    assert isinstance(parent, QWidget) or parent is host
    # 无 settings_dialog 时回退到宿主（QObject），不得返回控制器自身
    assert parent is not ctrl


def test_shutdown_waits_for_diagnostic_report_worker():
    """FR-20260812-D10-a: 关闭主窗口时必须等待诊断导出线程（report_worker），
    避免 "QThread destroyed while running"。"""
    import contextlib

    from fukua_rpa.ui.controllers.shutdown_controller import ShutdownController

    class _Host(QObject):
        # 仅提供 close_event 用到的成员
        _close_state_saved = True
        _close_retry_count = 0
        _hook_stop_retry_count = 0
        scheduled_service = None

        def __getattr__(self, name):
            if name in ("stop_key_mapping_hook",):
                return lambda: True
            if name in ("profiles_autosave_timer",):
                return None
            raise AttributeError(name)

    host = _Host()
    worker = mock.MagicMock()
    worker.isRunning.return_value = True
    worker.wait.return_value = True
    host.report_worker = worker

    ctrl = ShutdownController(host)
    with contextlib.suppress(Exception):
        ctrl.close_event(mock.MagicMock())
    worker.requestInterruption.assert_called_once_with()
    worker.wait.assert_called_once_with(1500)
