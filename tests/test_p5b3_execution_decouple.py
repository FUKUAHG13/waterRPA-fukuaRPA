"""P5b-3 守卫测试：execution_controller 脱离具体 Qt 控件 / 私有态。

验证内容：
1. ExecutionController 可在**无 QApplication** 环境下实例化（证明不再依赖具体宿主窗口）。
2. 原本直触具体 Qt 控件 / 私有属性的逻辑，现在只调用 HostView 的封装方法
   （set_*_enabled / task_list_* / is_mini_checked / current_log_level_text /
   show_*_message / show_*_tooltip / show_debug_variables_dialog /
   set|is_runtime_editing_locked_state / close_after_worker），由 StubHost 记录调用。
3. 源码层面不再出现直触控件 / 私有态 / QMessageBox / QCursor / QToolTip 的反模式
   （与 P5b-2 的源码级断言同口径，确保 decoupling 真实落地而非仅类型层面）。
4. HostView 协议已收窄：移除的 7 个仅 execution_controller 消费的成员已不在协议内，
   新增的 27 个封装方法已存在。

本测试刻意不创建任何真实窗口、不创建 QApplication，proof 的是"控制器只依赖接口"。
"""
from __future__ import annotations

import inspect
import re
from typing import Any, get_protocol_members

import pytest

from fukua_rpa.ui.controllers.execution_controller import ExecutionController
from fukua_rpa.ui.controllers.host_view import HostView


class _StubRunController:
    def __init__(self, *, pause=True, cont=True, step=True, active=True):
        self._pause = pause
        self._cont = cont
        self._step = step
        self._active = active
        self.calls: list[str] = []

    def pause_at_next_step(self):
        self.calls.append("pause_at_next_step")
        return self._pause

    def continue_run(self):
        self.calls.append("continue_run")
        return self._cont

    def step_over(self):
        self.calls.append("step_over")
        return self._step

    @property
    def is_active(self):
        return self._active

    def stop(self, *a, **k):
        self.calls.append("stop")

    def start(self, *a, **k):
        self.calls.append("start")
        return (True, None)


class _StubFeedback:
    recognition_test_active = False


class _StubEngine:
    def get_cmd_name(self, t):
        return "cmd"

    def apply_to(self, cfg):
        return None


class _StubPerf:
    def reset(self):
        return None


class _StubLogPolicy:
    def allows(self, level):
        return True


class _StubProfileHotkeyController:
    """host.profile_hotkey_controller 的存根。

    必须是**对象**，不能交给 _StubHost.__getattr__ 兜底：生产代码走的是
    ``host.profile_hotkey_controller.on_run_finished()``，即先取属性、再调其方法。
    而 __getattr__ 返回的是记录型 no-op **函数**，函数没有 .on_run_finished，
    只会在执行到那一行时炸成 AttributeError。
    """

    def __init__(self, host):
        self._host = host

    def on_run_finished(self):
        self._host._record("profile_hotkey_controller.on_run_finished")


class _StubWorkflowReviewController:
    """host.workflow_review_controller 的存根（同样必须是对象）。

    与真实实现的返回语义对齐：
    * ``validate_tasks`` 返回错误串；假值（None/""）表示校验通过。
    * ``confirm_loop_risks`` 返回 bool；True 表示用户确认继续。
    默认取"一路放行"，让不关心该分支的用例不被卡住；需要走拒绝分支的用例
    可通过 _StubHost 的构造参数改写。
    """

    def __init__(self, host, *, validate_error=None, confirm_loop=True):
        self._host = host
        self._validate_error = validate_error
        self._confirm_loop = confirm_loop

    def validate_tasks(self, tasks, cfg=None):
        self._host._record("workflow_review_controller.validate_tasks", tasks, cfg)
        return self._validate_error

    def confirm_loop_risks(self, tasks, cfg):
        self._host._record("workflow_review_controller.confirm_loop_risks", tasks, cfg)
        return self._confirm_loop


class _StubSettingsController:
    """Records calls made through host.settings_controller.*"""

    def __init__(self, host):
        self._host = host

    def update_debug_ui_visibility(self):
        self._host._record("update_debug_ui_visibility")

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            self._host._record(f"settings_controller.{name}", *args, **kwargs)
            return None
        return _noop


class _StubHost:
    """实现了 execution_controller 所需 HostView 表面的存根；记录所有调用。

    只暴露"意图明确"的封装方法，不持有任何真实 Qt 控件，因此控制器逻辑可在
    无 GUI 环境下被完整驱动与断言。
    """

    def __init__(
        self,
        *,
        settings_mode="advanced",
        mini=False,
        close_after=False,
        task_count=0,
        locked=False,
        run_ctrl: _StubRunController | None = None,
        validate_error=None,
        confirm_loop=True,
    ):
        self._settings_mode = settings_mode
        self._mini = mini
        self._close = close_after
        self._task_count = task_count
        self._locked = locked
        self._rec: dict[str, list[tuple[tuple, dict]]] = {}
        # 真实属性（execution_controller 会作为值读取，不能是被调用对象）
        self.runtime_ready = True
        self.current_profile_name = "默认方案"
        self.base_dir = "/tmp"
        self.feedback_controller = _StubFeedback()
        self.settings_controller = _StubSettingsController(self)
        self.run_controller = run_ctrl or _StubRunController()
        self.engine = _StubEngine()
        self.ui_performance = _StubPerf()
        self._log_policy = _StubLogPolicy()
        # 「对象型」host 依赖：生产代码以 host.<name>.<method>() 使用它们，
        # 必须显式建模，不能落到 __getattr__ 的 no-op 函数上。
        # test_stub_host_models_every_object_dependency 会锁死这份清单的完整性。
        self.profile_hotkey_controller = _StubProfileHotkeyController(self)
        self.workflow_review_controller = _StubWorkflowReviewController(
            self, validate_error=validate_error, confirm_loop=confirm_loop
        )
        # 这些属性会通过 getattr(host, name, default) 探测；必须作为真实属性存在，
        # 否则 __getattr__ 返回的 no-op 会覆盖默认值（set()/None）导致崩溃。
        self.task_config_dialogs: set = set()
        self.plugin_registry = None
        self.scheduled_service = None
        # set_runtime_editing_locked 通过 getattr(host, name, None) 遍历这些控件名，
        # 并对结果调用 isEnabled()/setEnabled()。设为真实 None 属性使其正确跳过。
        for _name in (
            "profile_combo",
            "new_profile_btn",
            "duplicate_profile_btn",
            "rename_profile_btn",
            "delete_profile_btn",
            "merge_profile_btn",
            "settings_btn",
            "key_mapping_action",
            "add_step_btn",
            "insert_step_btn",
            "redo_btn",
            "record_btn",
            "region_btn",
            "region_help_btn",
            "preview_points_btn",
            "script_check_btn",
            "breakpoint_toggle_btn",
            "loop_combo",
            "loop_val_edit",
            "mini_chk",
            "top_chk",
            "settings_actions",
            "settings_mode_combo",
            "fast_response_tip",
            "settings_scroll",
            "settings_dialog",
            "key_mapping_rows",
        ):
            setattr(self, _name, None)

    # ---- 调用记录工具 ----
    def _record(self, name, *args, **kwargs):
        self._rec.setdefault(name, []).append((args, kwargs))

    def _called(self, name) -> bool:
        return name in self._rec and len(self._rec[name]) > 0

    def _last_args(self, name):
        return self._rec[name][-1]

    # ---- 直触控件处收口后的封装方法（断言目标）----
    def set_debug_pause_enabled(self, enabled):
        self._record("set_debug_pause_enabled", enabled)

    def set_debug_continue_enabled(self, enabled):
        self._record("set_debug_continue_enabled", enabled)

    def set_debug_next_enabled(self, enabled):
        self._record("set_debug_next_enabled", enabled)

    def set_debug_variables_enabled(self, enabled):
        self._record("set_debug_variables_enabled", enabled)

    def set_start_enabled(self, enabled):
        self._record("set_start_enabled", enabled)

    def set_single_step_enabled(self, enabled):
        self._record("set_single_step_enabled", enabled)

    def set_stop_enabled(self, enabled):
        self._record("set_stop_enabled", enabled)

    def is_mini_checked(self):
        self._record("is_mini_checked")
        return self._mini

    def task_list_row_count(self):
        self._record("task_list_row_count")
        return self._task_count

    def task_list_item(self, index):
        self._record("task_list_item", index)
        return object()

    def task_list_item_widget(self, item):
        self._record("task_list_item_widget", item)
        return None

    def task_list_set_current_item(self, item):
        self._record("task_list_set_current_item", item)

    def task_list_scroll_to_item(self, item):
        self._record("task_list_scroll_to_item", item)

    def task_list_schedule_virtual_refresh(self, immediate=False):
        self._record("task_list_schedule_virtual_refresh", immediate=immediate)

    def task_list_drag_drop_mode(self):
        self._record("task_list_drag_drop_mode")
        return "NoDragDrop"

    def set_task_list_drag_drop_mode(self, mode):
        self._record("set_task_list_drag_drop_mode", mode)

    def current_log_level_text(self):
        self._record("current_log_level_text")
        return "普通"

    def show_information_message(self, title, text):
        self._record("show_information_message", title, text)

    def show_critical_message(self, title, text):
        self._record("show_critical_message", title, text)

    def show_warning_message(self, title, text):
        self._record("show_warning_message", title, text)

    def show_debug_pause_tooltip(self, text):
        self._record("show_debug_pause_tooltip", text)

    def show_start_tooltip(self, text):
        self._record("show_start_tooltip", text)

    def show_single_step_tooltip(self, text):
        self._record("show_single_step_tooltip", text)

    def show_debug_variables_dialog(self, step, variables):
        self._record("show_debug_variables_dialog", step, variables)

    def set_runtime_editing_locked_state(self, locked):
        self._locked = bool(locked)
        self._record("set_runtime_editing_locked_state", locked)

    def is_runtime_editing_locked_state(self):
        self._record("is_runtime_editing_locked_state")
        return self._locked

    def close_after_worker(self):
        self._record("close_after_worker")
        return self._close

    # ---- 其它协议成员：纯记录 no-op（其返回值在受测路径上不被关键使用）----
    def current_settings_mode(self):
        self._record("current_settings_mode")
        return self._settings_mode

    def current_log_policy(self):
        return self._log_policy

    def __getattr__(self, name: str) -> Any:
        # 未在上方显式定义的协议成员 → 返回记录型 no-op
        def _noop(*args, **kwargs):
            self._record(name, *args, **kwargs)
            return None

        return _noop


# ---------------------------------------------------------------------------
# 1) 无 GUI 实例化
# ---------------------------------------------------------------------------
def test_execution_controller_instantiates_without_qt():
    host = _StubHost()
    ctrl = ExecutionController(host)
    assert ctrl.host is host


# ---------------------------------------------------------------------------
# 2) clear_debug_pause_highlight 只经由 task_list_* 封装方法访问列表
# ---------------------------------------------------------------------------
def test_clear_debug_pause_highlight_uses_wrappers():
    host = _StubHost(task_count=3)
    ctrl = ExecutionController(host)
    ctrl.clear_debug_pause_highlight()
    assert host._called("task_list_row_count")
    assert host._called("task_list_item")
    assert host._called("task_list_item_widget")
    assert host._called("update_debug_ui_visibility")
    # 不应有任何"直接控件属性"被读取（存根无这些属性，getattr 也不会产生它们）
    assert not host._called("debug_pause_btn")
    assert not host._called("task_list")  # 注意：task_list 作为控件本身不应被直触


# ---------------------------------------------------------------------------
# 3) set_runtime_editing_locked 收口私有态读写与 dragDropMode
# ---------------------------------------------------------------------------
def test_set_runtime_editing_locked_uses_wrappers():
    host = _StubHost(locked=False)
    ctrl = ExecutionController(host)
    ctrl.set_runtime_editing_locked(True)
    assert host._called("is_runtime_editing_locked_state")
    assert host._called("task_list_drag_drop_mode")
    assert host._called("set_task_list_drag_drop_mode")
    assert host._called("set_runtime_editing_locked_state")
    # 断言确实写入了锁定态（经由封装方法）
    last = host._last_args("set_runtime_editing_locked_state")
    assert last[0][0] is True
    # 解锁
    ctrl.set_runtime_editing_locked(False)
    last2 = host._last_args("set_runtime_editing_locked_state")
    assert last2[0][0] is False


# ---------------------------------------------------------------------------
# 4) handle_debug_event(paused) → 正确的按钮使能与列表定位封装调用
# ---------------------------------------------------------------------------
def test_handle_debug_event_paused_sets_buttons_and_list():
    host = _StubHost(task_count=5)
    ctrl = ExecutionController(host)
    ctrl.handle_debug_event({"state": "paused", "step": 3, "variables": {"x": 1}})
    assert host._called("set_debug_pause_enabled")
    assert host._last_args("set_debug_pause_enabled")[0][0] is False
    assert host._called("set_debug_continue_enabled")
    assert host._last_args("set_debug_continue_enabled")[0][0] is True
    assert host._called("set_debug_next_enabled")
    assert host._last_args("set_debug_next_enabled")[0][0] is True
    assert host._called("set_debug_variables_enabled")
    assert host._last_args("set_debug_variables_enabled")[0][0] is True
    # 列表定位封装
    assert host._called("task_list_row_count")
    assert host._called("task_list_item")
    assert host._called("task_list_set_current_item")
    assert host._called("task_list_scroll_to_item")
    assert host._called("task_list_schedule_virtual_refresh")
    assert host._last_args("task_list_schedule_virtual_refresh")[1] == {"immediate": True}


# ---------------------------------------------------------------------------
# 5) _enter_running_ui_state → 进入运行态的按钮/最小化封装
# ---------------------------------------------------------------------------
def test_enter_running_ui_state_sets_running_widgets():
    host = _StubHost(settings_mode="advanced", mini=False)
    ctrl = ExecutionController(host)
    ctrl._enter_running_ui_state({}, source="manual")
    assert host._last_args("set_start_enabled")[0][0] is False
    assert host._last_args("set_single_step_enabled")[0][0] is False
    assert host._last_args("set_stop_enabled")[0][0] is True
    assert host._last_args("set_debug_pause_enabled")[0][0] is True  # advanced
    assert host._last_args("set_debug_continue_enabled")[0][0] is False
    assert host._last_args("set_debug_next_enabled")[0][0] is False
    assert host._last_args("set_debug_variables_enabled")[0][0] is False
    assert host._called("is_mini_checked")
    assert host._called("maybe_show_emergency_stop_tip_once")
    assert host._called("show_running_status_overlay")
    assert not host._called("showMinimized")


def test_enter_running_ui_state_minimizes_when_mini_checked():
    host = _StubHost(settings_mode="advanced", mini=True)
    ctrl = ExecutionController(host)
    ctrl._enter_running_ui_state({}, source="manual")
    assert host._called("showMinimized")


# ---------------------------------------------------------------------------
# 6) on_finish → 进入空闲态的按钮封装（close_after=False 分支，避免调度回调）
# ---------------------------------------------------------------------------
def test_on_finish_restores_idle_widgets():
    host = _StubHost(close_after=False)
    ctrl = ExecutionController(host)
    ctrl.on_finish()
    assert host._last_args("set_start_enabled")[0][0] is True
    assert host._last_args("set_single_step_enabled")[0][0] is True
    assert host._last_args("set_stop_enabled")[0][0] is False
    assert host._last_args("set_debug_pause_enabled")[0][0] is False
    assert host._last_args("set_debug_continue_enabled")[0][0] is False
    assert host._last_args("set_debug_next_enabled")[0][0] is False
    assert host._last_args("set_debug_variables_enabled")[0][0] is False
    assert host._called("close_after_worker")
    assert host._called("hide_running_status_overlay")
    assert host._called("refresh_scale_memory_status")
    # 运行结束必须通知方案热键控制器解除"运行中"状态，否则热键会一直被判为占用。
    assert host._called("profile_hotkey_controller.on_run_finished")


# ---------------------------------------------------------------------------
# 6b) on_worker_error 同样要解除热键占用（历史上这条路径完全无覆盖）
# ---------------------------------------------------------------------------
def test_on_worker_error_releases_hotkey_and_reports():
    """执行线程异常时也必须调用 on_run_finished，否则热键永久卡在"运行中"。

    这条分支此前零覆盖：execution_controller 里有两处
    ``profile_hotkey_controller.on_run_finished()``（on_finish 与 on_worker_error），
    存根漂移时只有被覆盖的那一处会报错，另一处继续静默带病。
    """
    host = _StubHost()
    ctrl = ExecutionController(host)
    ctrl.on_worker_error("boom")
    assert host._called("profile_hotkey_controller.on_run_finished")
    assert host._called("cancel_active_script_prompt")
    assert host._called("append_application_log")
    assert host._last_args("show_critical_message")[0][1] == "boom"


# ---------------------------------------------------------------------------
# 7) show_debug_variables → 经由 show_debug_variables_dialog 封装
# ---------------------------------------------------------------------------
def test_show_debug_variables_uses_dialog_wrapper():
    host = _StubHost()
    ctrl = ExecutionController(host)
    ctrl.debug_paused_step = 4
    ctrl.debug_variable_values = {"a": 1, "b": 2}
    ctrl.show_debug_variables()
    assert host._called("show_debug_variables_dialog")
    args, _ = host._last_args("show_debug_variables_dialog")
    assert args[0] == 4
    assert args[1] == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# 8) 调试按钮控制 → 封装方法
# ---------------------------------------------------------------------------
def test_pause_debug_run_uses_wrappers():
    host = _StubHost(settings_mode="advanced")
    ctrl = ExecutionController(host)
    ctrl.pause_debug_run()
    assert host._called("set_debug_pause_enabled")
    assert host._last_args("set_debug_pause_enabled")[0][0] is False
    assert host._called("show_debug_pause_tooltip")

    # 非 advanced 模式应直接返回，不触碰封装
    host2 = _StubHost(settings_mode="simple")
    ctrl2 = ExecutionController(host2)
    ctrl2.pause_debug_run()
    assert not host2._called("set_debug_pause_enabled")
    assert not host2._called("show_debug_pause_tooltip")


def test_continue_and_step_over_use_wrappers():
    host = _StubHost()
    ctrl = ExecutionController(host)
    ctrl.continue_debug_run()
    assert host._last_args("set_debug_continue_enabled")[0][0] is False
    assert host._last_args("set_debug_next_enabled")[0][0] is False
    assert host._last_args("set_debug_variables_enabled")[0][0] is False

    host2 = _StubHost()
    ctrl2 = ExecutionController(host2)
    ctrl2.step_over_debug_run()
    assert host2._last_args("set_debug_continue_enabled")[0][0] is False
    assert host2._last_args("set_debug_next_enabled")[0][0] is False
    assert host2._last_args("set_debug_variables_enabled")[0][0] is False


def test_stop_task_uses_wrappers():
    host = _StubHost()
    ctrl = ExecutionController(host)
    ctrl.stop_task()
    assert host._called("set_stop_enabled")
    assert host._last_args("set_stop_enabled")[0][0] is False
    assert host._called("set_debug_pause_enabled")
    assert host._last_args("set_debug_pause_enabled")[0][0] is False
    for name in (
        "set_debug_continue_enabled",
        "set_debug_next_enabled",
        "set_debug_variables_enabled",
    ):
        assert host._last_args(name)[0][0] is False
    assert "stop" in host.run_controller.calls


# ---------------------------------------------------------------------------
# 9) start_task / run_selected_step_once 在 runtime_ready=False 时只走 tooltip 封装
# ---------------------------------------------------------------------------
def test_start_task_runtime_not_ready_uses_tooltip_wrapper():
    host = _StubHost()
    host.runtime_ready = False
    ctrl = ExecutionController(host)
    ctrl.start_task()
    assert host._called("show_start_tooltip")
    # 不应进入 _start_task_impl（无引擎启动相关调用）
    assert not host._called("_start_task_impl")


def test_run_selected_step_once_runtime_not_ready_uses_tooltip_wrapper():
    host = _StubHost()
    host.runtime_ready = False
    ctrl = ExecutionController(host)
    ctrl.run_selected_step_once()
    assert host._called("show_single_step_tooltip")
    assert not host._called("_launch_prepared_tasks")


# ---------------------------------------------------------------------------
# 10) 源码级断言：execution_controller 不再直触控件 / 私有态 / Qt 消息框
# ---------------------------------------------------------------------------
FORBIDDEN_PATTERNS = [
    "self.host.debug_pause_btn",
    "self.host.debug_continue_btn",
    "self.host.debug_next_btn",
    "self.host.debug_variables_btn",
    "self.host.start_btn",
    "self.host.single_step_btn",
    "self.host.stop_btn",
    "self.host.mini_chk",
    "self.host.log_level_combo",
    "self.host.task_list.",
    "self.host._close_after_worker",
    "self.host._runtime_editing_locked",
    # FR-20260811-D10: 回环模式——controller 必须直接调自身 _launch_prepared_tasks，
    # 不得经 host(forwarder) 转发回自身（纯透传回环，架构整洁）。
    "self.host._launch_prepared_tasks",
    "QMessageBox(",
    "QToolTip.",
    "QCursor.",
]


@pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
def test_no_direct_widget_or_qt_access_in_source(pattern):
    src = inspect.getsource(ExecutionController)
    assert pattern not in src, f"execution_controller 仍含反模式: {pattern}"


# ---------------------------------------------------------------------------
# 10b) 存根完整性闸门：对象型 host 依赖必须显式建模
# ---------------------------------------------------------------------------
def test_stub_host_models_every_object_dependency():
    """凡是生产代码以 ``host.<name>.<method>()`` 使用的成员，_StubHost 必须提供真实属性。

    _StubHost.__getattr__ 会给任何未建模的成员返回一个记录型 no-op **函数**。
    函数没有方法，因此 ``host.X.y()`` 只会在恰好被某条用例走到的那一行炸成
    AttributeError；没被走到的路径则毫无声息。历史上这正是两处漂移的成因：
    ``profile_hotkey_controller``（被 on_finish 走到 → 变红）与
    ``workflow_review_controller``（无用例走到 → 静默潜伏）。

    这里做源码级扫描，把"新增一个对象型依赖就必须同步扩存根"钉死，
    让漏建模在闸门处即时暴露，而不是等某条用例偶然踩中。
    """
    src = inspect.getsource(ExecutionController)
    accessed = set(re.findall(r"self\.host\.([A-Za-z_]\w*)\.[A-Za-z_]", src))
    assert accessed, "未扫描到任何 self.host.<name>.<attr> 用法，正则或源码结构已变，闸门失效"

    host = _StubHost()
    modeled = set(vars(host))
    missing = sorted(name for name in accessed if name not in modeled)

    assert not missing, (
        "以下 host 成员被生产代码当作**对象**使用（host.X.y()），"
        "但 _StubHost 未显式建模，会落到 __getattr__ 的 no-op 函数上：\n  "
        + "\n  ".join(missing)
        + "\n请在 _StubHost.__init__ 中补上对应的存根对象。"
    )


# ---------------------------------------------------------------------------
# 11) HostView 协议已收窄：移除的成员不在，新增的成员在
# ---------------------------------------------------------------------------
REMOVED_MEMBERS = {
    "debug_pause_btn",
    "debug_continue_btn",
    "debug_next_btn",
    "debug_variables_btn",
    "single_step_btn",
    "mini_chk",
    "_close_after_worker",
}
ADDED_MEMBERS = {
    "set_debug_pause_enabled",
    "set_debug_continue_enabled",
    "set_debug_next_enabled",
    "set_debug_variables_enabled",
    "set_start_enabled",
    "set_single_step_enabled",
    "set_stop_enabled",
    "is_mini_checked",
    "task_list_row_count",
    "task_list_item",
    "task_list_item_widget",
    "task_list_set_current_item",
    "task_list_scroll_to_item",
    "task_list_schedule_virtual_refresh",
    "task_list_drag_drop_mode",
    "set_task_list_drag_drop_mode",
    "current_log_level_text",
    "show_information_message",
    "show_critical_message",
    "show_warning_message",
    "show_debug_pause_tooltip",
    "show_start_tooltip",
    "show_single_step_tooltip",
    "show_debug_variables_dialog",
    "set_runtime_editing_locked_state",
    "is_runtime_editing_locked_state",
    "close_after_worker",
}


def test_protocol_narrows_removed_members_gone():
    members = get_protocol_members(HostView)
    for name in REMOVED_MEMBERS:
        assert name not in members, f"协议应已移除 {name}"


def test_protocol_narrows_added_members_present():
    members = get_protocol_members(HostView)
    for name in ADDED_MEMBERS:
        assert name in members, f"协议应已新增 {name}"


def test_protocol_retains_shared_members():
    """start_btn / stop_btn / log_level_combo / task_list 等共享成员应保留。"""
    members = get_protocol_members(HostView)
    for name in ("start_btn", "stop_btn", "log_level_combo", "task_list"):
        assert name in members, f"共享成员 {name} 不应被移除"
