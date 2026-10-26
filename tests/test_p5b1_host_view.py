"""P5b-1 守卫测试：HostView 协议类型解耦。

验证内容：
1. HostView 是 runtime_checkable 协议，并包含控制器依赖的稳定关键接口。
2. 用协议成员动态构建的 StubHost 满足 isinstance(HostView) —— 证明契约可由“非 RPAWindow”对象实现，
   控制器因此依赖接口而非具体宿主窗口。
3. 16 个具体控制器的 __init__ 把 host 参数、host 属性把返回类型注解为 HostView。
4. 全部控制器模块可在无 GUI 环境下导入（无循环导入；host_view 仅依赖 typing）。

本测试刻意不实例化任何真实窗口，proof 的是“类型契约”这一层的 decoupling。
"""
from typing import get_protocol_members

import pytest

from fukua_rpa.ui.controllers.host_view import HostView

# (模块名, 类名) —— 使用 HostView 的具体控制器 / mixin
CONTROLLERS = [
    ("fukua_rpa.ui.controllers.diagnostics_controller", "DiagnosticsController"),
    ("fukua_rpa.ui.controllers.execution_controller", "ExecutionController"),
    ("fukua_rpa.ui.controllers.feedback_controller", "VisualFeedbackController"),
    ("fukua_rpa.ui.controllers.hotkey_controller", "HotkeyController"),
    ("fukua_rpa.ui.controllers.log_controller", "LogController"),
    ("fukua_rpa.ui.controllers.mapping_controller", "MappingController"),
    ("fukua_rpa.ui.controllers.profile_controller", "ProfileController"),
    (
        "fukua_rpa.ui.controllers.profile_hotkey_controller",
        "ProfileHotkeyController",
    ),
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


def test_host_view_is_runtime_checkable():
    assert getattr(HostView, "_is_runtime_protocol", False) is True


def test_host_view_contains_stable_controller_contracts():
    members = get_protocol_members(HostView)
    # 精确成员总数会因新增控制器接口而自然变化，无法证明解耦是否仍成立。
    # 守卫真正不可缺失的跨控制器边界，新增能力时再显式扩充此集合。
    required = {
        "active_key_mappings",
        "auto_clicker_configured_hotkey_bindings",
        "auto_clicker_hotkey_bindings",
        "auto_clicker_is_running",
        "hotkey_controller",
        "profile_hotkey_controller",
        "profile_model",
        "run_controller",
        "start_auto_clicker_from_hotkey",
        "start_task",
        "stop_auto_clicker",
        "stop_task",
    }
    assert required <= members


def test_stub_host_satisfies_protocol():
    """一个不依赖 RPAWindow 的存根即可满足 HostView —— 这正是 decoupling 的基石。"""
    members = get_protocol_members(HostView)

    class StubHost:
        def __init__(self):
            for name in members:
                setattr(self, name, lambda *a, **k: None)

    assert isinstance(StubHost(), HostView)


def test_controllers_annotate_host_as_hostview():
    import_errors = []
    for modname, clsname in CONTROLLERS:
        try:
            module = __import__(modname, fromlist=[clsname])
        except Exception as exc:  # noqa: BLE001
            import_errors.append(f"{modname}: {exc!r}")
            continue
        cls = getattr(module, clsname)

        # 仅当类自行定义 __init__ 时校验 host 参数注解
        # （如 WorkflowContextMixin 这类 mixin 自身不定义 __init__，host 由具象控制器注入）
        init = cls.__dict__.get("__init__")
        if init is not None and hasattr(init, "__annotations__"):
            init_ann = init.__annotations__
            assert "host" in init_ann, f"{clsname}.__init__ 缺少 host 参数注解"
            assert init_ann["host"] in ("HostView", HostView), (
                f"{clsname}.__init__ host 注解为 {init_ann['host']!r}，应为 HostView"
            )

        host_attr = getattr(cls, "host", None)
        assert host_attr is not None, f"{clsname} 没有 host 属性"
        ret_ann = (
            host_attr.fget.__annotations__
            if isinstance(host_attr, property)
            else host_attr.__annotations__
        )
        assert "return" in ret_ann, f"{clsname}.host 缺少返回类型注解"
        assert ret_ann["return"] in ("HostView", HostView), (
            f"{clsname}.host 返回注解为 {ret_ann['return']!r}，应为 HostView"
        )

    assert not import_errors, "以下控制器模块导入失败:\n" + "\n".join(import_errors)


def test_host_view_has_no_runtime_dependency():
    """HostView 协议模块不得引入任何 Qt / 业务运行时依赖（仅 typing）。"""
    import inspect

    import fukua_rpa.ui.controllers.host_view as hv

    src = inspect.getsource(hv)
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("from __future__ import"):
            continue
        if s.startswith("from typing import") or s.startswith("import typing"):
            continue
        if s.startswith("from ") or s.startswith("import "):
            pytest.fail(f"host_view.py 引入了非 typing 依赖: {s}")
