# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Run preparation, debug controls and execution UI lifecycle."""

from __future__ import annotations

import contextlib
import copy
import weakref
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QAbstractItemView

from ...log_policy import (
    LOG_CRITICAL,
    LOG_RUN,
)
from ...logging_service import GLOBAL_CONFIG, build_run_log_context, write_log
from ...plugin_actions import collect_plugin_dependencies
from ...plugin_registry import PluginRegistryError, PluginStatus
from ...privacy import private_debug_value, sensitive_text_metadata
from ...run_config import EngineRunConfig, RunConfigError, RunRequest
from ...task_model import config_bool, parse_float_text, task_is_disabled
from ..lifecycle import schedule_widget_callback

if TYPE_CHECKING:
    from .host_view import HostView


# FR-20260812 建议-架构-2: 运行期编辑锁控件清单抽为模块级常量（单一事实来源）。
# 运行期间这些控件被禁用，防止用户修改方案/设置/步骤；清单改动只需改这一处，
# 测试与实现共用同一常量避免漂移。
RUNTIME_EDIT_LOCK_WIDGETS = (
    "profile_combo",
    "new_profile_btn",
    "duplicate_profile_btn",
    "rename_profile_btn",
    "delete_profile_btn",
    "merge_profile_btn",
    "settings_btn",
    # 映射编辑已迁到「工具 → 按键映射」；运行中禁止打开该编辑窗口（FR-20261021-004）。
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
)


class ExecutionController:
    """Coordinate the existing runner without duplicating engine behavior."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.start_in_progress = False
        self.debug_paused_step = 0
        self.debug_variable_values = {}
        self._runtime_widget_states = None
        # ADV-20261006-022: per-control enabled state of the settings dialog's
        # shortcut/mapping rows, captured when a run starts and replayed on unlock.
        self._settings_row_states: list[tuple[Any, bool]] = []
        # FR-20261022-001/002: 迁出设置对话框的编辑容器（映射工具窗口的映射区、
        # 方案快捷键对话框）原启用态；键为 id()，避免窗口在运行中被替换后错配。
        self._secondary_editor_states: dict[int, tuple[Any, bool]] | None = None
        self._task_list_drag_mode = None
        self._run_plugin_deps: tuple[str, ...] = ()
        self._watchdog_release_connected = False

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("执行窗口已销毁")
        return host

    def _auto_clicker_is_running(self):
        """Read the optional tool state without treating mock/unknown values as active."""
        checker = getattr(self.host, "auto_clicker_is_running", None)
        return bool(checker and checker() is True)

    def auto_clicker_is_running(self):
        """Public accessor: does the standalone clicker hold input ownership?

        FR-20260809-1506: the scheduled-run coordinator consults this so a
        trigger that fires while the clicker is running enters the conflict
        policy path instead of being silently dropped by the launch boundary.
        """
        return self._auto_clicker_is_running()

    def launch_blocked_reason(self) -> str | None:
        """Return why a launch must not start right now, or ``None`` to proceed.

        ADV-20261006-002 + FR-20261006-006: this predicate was previously written
        out four times, and the fourth copy — ``prepare_and_launch_profile``, used
        by both scheduled tasks and profile hotkeys — was **missing** the
        recognition-test term entirely. A recognition test injects real input, so a
        schedule firing mid-recognition had two input owners at once. Centralizing
        the predicate is what prevents the next launch path from omitting a term.

        Terms, in the order the user should be told about them:

        * 连点器正在运行 — the standalone clicker owns input; both tool and script
          are mutually exclusive at any moment (设计基准「独立输入工具」).
        * 启动/识别测试进行中 — a run is starting, already active, or a recognition
          test is injecting input.
        """
        if self._auto_clicker_is_running():
            return "连点器正在运行，请先停止连点器"
        if (
            self.start_in_progress
            or self.host.run_controller.is_active
            or self.host.feedback_controller.recognition_test_active
        ):
            # 这些分支此前是静默 ``return``：手动运行、单步执行与计划/热键启动
            # 都不给用户任何提示。保留静默可避免"正在运行时反复弹提示"，因此这里
            # 返回 None 表示"已阻止但无需打扰用户"，只有需要用户先停止连点器的
            # 情形才交由调用方显示提示。
            return ""
        return None

    def is_launch_blocked(self) -> bool:
        """``True`` when a launch must not start (empty string = blocked silently)."""
        return self.launch_blocked_reason() is not None

    def clear_debug_pause_highlight(self):
        self.debug_paused_step = 0
        for index in range(self.host.task_list_row_count()):
            item = self.host.task_list_item(index)
            widget = self.host.task_list_item_widget(item)
            if widget and hasattr(widget, "set_debug_paused"):
                widget.set_debug_paused(False)
        self.host.settings_controller.update_debug_ui_visibility()

    def set_runtime_editing_locked(self, locked):
        host = self.host
        locked = bool(locked)
        if host.is_runtime_editing_locked_state() == locked:
            return
        widget_names = RUNTIME_EDIT_LOCK_WIDGETS
        if locked:
            self._runtime_widget_states = []
            for name in widget_names:
                widget = getattr(host, name, None)
                if widget is None:
                    continue
                self._runtime_widget_states.append((widget, widget.isEnabled()))
                widget.setEnabled(False)
            self._task_list_drag_mode = host.task_list_drag_drop_mode()
            host.set_task_list_drag_drop_mode(QAbstractItemView.NoDragDrop)
        else:
            for widget, was_enabled in self._runtime_widget_states or ():
                with contextlib.suppress(RuntimeError):
                    widget.setEnabled(was_enabled)
            self._runtime_widget_states = None
            if self._task_list_drag_mode is not None:
                host.set_task_list_drag_drop_mode(self._task_list_drag_mode)
                self._task_list_drag_mode = None
        host.set_runtime_editing_locked_state(locked)
        for index in range(host.task_list_row_count()):
            row = host.task_list_item_widget(host.task_list_item(index))
            if row is not None and hasattr(row, "set_runtime_editing_enabled"):
                row.set_runtime_editing_enabled(not locked)
        for dialog in list(getattr(host, "task_config_dialogs", set())):
            try:
                dialog.setEnabled(not locked)
            except RuntimeError:
                host.task_config_dialogs.discard(dialog)
        self._set_settings_dialog_read_only(host, locked)
        self._set_secondary_editors_read_only(host, locked)

    @staticmethod
    def _secondary_editor_targets(host):
        """Return containers that edit global settings outside the settings dialog.

        FR-20261022-001/002: 只读边界过去等于「控件名清单 + 设置对话框子树」。
        映射编辑区迁到独立工具窗口后不再位于该子树；方案快捷键对话框本身是
        ``parent=None`` 的顶层窗口，也不在子树内。两者编辑的仍是全局设置
        （``ARCHITECTURE.md`` #62），必须在运行期整块只读。
        """
        targets = []
        window = getattr(host, "_key_mapping_tool_window", None)
        section = getattr(window, "mapping_section", None) if window is not None else None
        if section is not None:
            targets.append(section)
        controller = getattr(host, "profile_hotkey_controller", None)
        dialog = getattr(controller, "_dialog", None) if controller is not None else None
        if dialog is not None:
            targets.append(dialog)
        return targets

    def _set_secondary_editors_read_only(self, host, locked: bool) -> None:
        """Disable/re-enable whole secondary editors instead of enumerating children.

        DESIGN: 整块 ``setEnabled`` 由 Qt 传递给子控件，因此「+ 添加映射」这类
        局部变量控件也被覆盖，不需要继续维护易漂移的控件名清单。子控件自身的
        启用标志不被改写，解锁后原本禁用的控件仍保持禁用。

        运行中新建的窗口（理论上入口已禁用，但测试或未来入口可能触达）不在
        加锁时的快照里，解锁时按默认可用恢复，不会被永久禁用。
        """
        targets = self._secondary_editor_targets(host)
        if locked:
            self._secondary_editor_states = {}
            for widget in targets:
                try:
                    self._secondary_editor_states[id(widget)] = (
                        widget,
                        widget.isEnabled(),
                    )
                    widget.setEnabled(False)
                except RuntimeError:
                    continue
            return
        states = self._secondary_editor_states or {}
        for widget in targets:
            try:
                record = states.get(id(widget))
                widget.setEnabled(record[1] if record is not None else True)
            except RuntimeError:
                continue
        self._secondary_editor_states = None

    def _set_settings_dialog_read_only(self, host, locked: bool) -> None:
        """ADV-20261006-022: an **already open** settings dialog goes read-only.

        ``show_settings_dialog`` refuses to open while a run is active, but a dialog
        opened *before* the run stayed fully editable: the user could rebind keys and
        window mappings mid-run, and those changes would only take effect on the next
        start — silently diverging from what the running script is using.

        Scope is deliberately narrow (the maintainer's boundary): only the shortcut /
        mapping editing rows go read-only, the dialog stays visible and closable, and
        the previous enabled state of each control is restored on unlock. Nothing
        about stop or run monitoring is touched.
        """
        # INVARIANT: 映射行归属宿主（`host.key_mapping_rows`），不是设置对话框对象。
        # 映射编辑区迁到「工具 → 按键映射」后对话框不再持有该属性，只查对话框会
        # 静默变成空操作（FR-20261021-004）。
        rows = getattr(host, "key_mapping_rows", None)
        if not rows:
            dialog = getattr(host, "settings_dialog", None)
            if dialog is None:
                return
            rows = getattr(dialog, "key_mapping_rows", None) or self._mapping_rows_of(
                dialog
            )
        if not rows:
            return
        if locked:
            self._settings_row_states = []
        for row in rows:
            widget = self._first_interactive_widget(row)
            if widget is None:
                continue
            with contextlib.suppress(RuntimeError):
                if locked:
                    self._settings_row_states.append((widget, widget.isEnabled()))
                    widget.setEnabled(False)
                elif self._settings_row_states:
                    was_enabled = self._settings_row_states.pop(0)[1]
                    widget.setEnabled(was_enabled)

    @staticmethod
    def _mapping_rows_of(dialog):
        """Find the shortcut/mapping row container across naming variants."""
        for name in ("mapping_rows", "key_mapping_container", "mapping_container"):
            rows = getattr(dialog, name, None)
            if rows:
                return list(rows)
        return []

    @staticmethod
    def _first_interactive_widget(row):
        """The checkbox/enabled switch of a mapping row, which gates the row."""
        widget = row.get("enabled") if isinstance(row, dict) else None
        return widget if widget is not None else None

    def show_debug_variables(self):
        self.host.show_debug_variables_dialog(
            self.debug_paused_step, self.debug_variable_values
        )

    def pause_debug_run(self):
        if self.host.current_settings_mode() != "advanced":
            return
        if self.host.run_controller.pause_at_next_step():
            self.host.set_debug_pause_enabled(False)
            self.host.show_debug_pause_tooltip("将在下一个实际执行步骤前暂停")

    def continue_debug_run(self):
        if self.host.run_controller.continue_run():
            self.host.set_debug_continue_enabled(False)
            self.host.set_debug_next_enabled(False)
            self.host.set_debug_variables_enabled(False)

    def step_over_debug_run(self):
        if self.host.run_controller.step_over():
            self.host.set_debug_continue_enabled(False)
            self.host.set_debug_next_enabled(False)
            self.host.set_debug_variables_enabled(False)

    def handle_debug_event(self, data):
        state = str(data.get("state", ""))
        step = max(0, int(data.get("step", 0) or 0))
        loop = max(0, int(data.get("loop", 0) or 0))
        command = str(data.get("command", "") or "")
        reason_text = {
            "breakpoint": "命中断点",
            "conditional_breakpoint": "条件断点成立",
            "breakpoint_condition_error": "断点条件无法求值",
            "pause_requested": "用户请求暂停",
            "step_over": "单步完成",
        }.get(str(data.get("reason", "")), "调试暂停")
        if state == "paused":
            self.host.clear_debug_pause_highlight()
            self.debug_paused_step = step
            self.host.settings_controller.update_debug_ui_visibility()
            variables = data.get("variables", {})
            self.debug_variable_values = (
                dict(variables) if isinstance(variables, dict) else {}
            )
            if 1 <= step <= self.host.task_list_row_count():
                item = self.host.task_list_item(step - 1)
                self.host.task_list_set_current_item(item)
                self.host.task_list_scroll_to_item(item)
                self.host.task_list_schedule_virtual_refresh(immediate=True)
                widget = self.host.ensure_task_item_widget(item)
                if widget and hasattr(widget, "set_debug_paused"):
                    widget.set_debug_paused(True)
            self.host.set_debug_pause_enabled(False)
            self.host.set_debug_continue_enabled(True)
            self.host.set_debug_next_enabled(True)
            self.host.set_debug_variables_enabled(True)
            self.host.update_running_status_overlay(
                {
                    "loop": loop,
                    "step": step,
                    "total": self.host.task_list_row_count(),
                    "cmd": f"已暂停 · {command}",
                }
            )
            detail = str(data.get("detail", "") or "")
            detail_text = (
                f"<br><span style='color:#92400E'>原因详情已脱敏（{sensitive_text_metadata(detail)}）</span>"
                if detail
                else ""
            )
            self.host.append_application_log(
                f"<font color='#D97706'><b>调试暂停：循环 #{loop} 第 {step} 步 "
                f"({command})，{reason_text}。</b>{detail_text}</font>",
                private_msg=(
                    f"<font color='#D97706'><b>调试暂停：循环 #{loop} 第 {step} 步 "
                    f"({command})，{reason_text}。</b>"
                    + (
                        "<br><span style='color:#92400E'>原因详情："
                        f"{private_debug_value(detail)}</span>"
                        if detail
                        else ""
                    )
                    + "</font>"
                ),
            )
        elif state in ("resumed", "cancelled"):
            self.host.clear_debug_pause_highlight()
            self.host.set_debug_continue_enabled(False)
            self.host.set_debug_next_enabled(False)
            self.host.set_debug_variables_enabled(False)
            self.host.set_debug_pause_enabled(
                state == "resumed" and self.host.run_controller.is_active
            )

    def start_task(self):
        if not self.host.runtime_ready:
            self.host.show_start_tooltip("识别运行库仍在准备，请稍候")
            return
        # ADV-20261006-002: 统一走launch_blocked_reason（此前此守卫被复制四份，
        # 计划/热键路径那份漏了识别测试项，见 FR-20261006-006）。
        if self.is_launch_blocked():
            reason = self.launch_blocked_reason()
            if reason:
                self.host.show_start_tooltip(reason)
            return
        self.start_in_progress = True
        try:
            # FR-20260809-D8-①: 收敛回环——原经 host._start_task_impl()（forwarder
            # 透传）再转发回自身 _start_task_impl，形成 4 层往返；直接调用等价。
            self._start_task_impl()
        finally:
            self.start_in_progress = False

    def _start_task_impl(self):
        cfg = self.host.get_current_ui_config()
        tasks = self.host.tasks_for_current_settings_mode(cfg.get("tasks", []))
        if not tasks:
            return

        err_msg = self.host.workflow_review_controller.validate_tasks(tasks, cfg)
        if err_msg:
            self.host.show_critical_message("指令语法错误", err_msg)
            return

        if not self.host.workflow_review_controller.confirm_loop_risks(tasks, cfg):
            return

        try:
            run_request = RunRequest.create(
                tasks,
                cfg,
                self.host.current_profile_name,
                plugin_dependencies=sorted(collect_plugin_dependencies(tasks)),
            )
            run_request.config.apply_to(self.host.engine)
            tasks = run_request.mutable_tasks()
        except RunConfigError as error:
            self.host.show_warning_message("错误", str(error))
            return

        # FR-20260811-D10: 直接调自身方法，消除冗余回环（见单步执行处注释）。
        self._launch_prepared_tasks(tasks, cfg, "引擎启动", plugin_dependencies=run_request.plugin_dependencies)

    def start_task_from_step_once(self, selected_index):
        """Start the first loop at one row without changing profile settings."""

        if not self.host.runtime_ready:
            self.host.show_start_tooltip("识别运行库仍在准备，请稍候")
            return False
        # ADV-20261006-002: 与 start_task 共用同一判据。
        if self.is_launch_blocked():
            reason = self.launch_blocked_reason()
            if reason:
                self.host.show_start_tooltip(reason)
            return False
        self.start_in_progress = True
        try:
            cfg = self.host.get_current_ui_config()
            tasks = self.host.tasks_for_current_settings_mode(
                cfg.get("tasks", [])
            )
            try:
                selected_index = int(selected_index)
            except (TypeError, ValueError):
                return False
            if not 0 <= selected_index < len(tasks):
                return False

            transient_cfg = copy.deepcopy(cfg)
            transient_cfg["initial_start_step"] = str(selected_index + 1)
            err_msg = self.host.workflow_review_controller.validate_tasks(
                tasks,
                transient_cfg,
            )
            if err_msg:
                self.host.show_critical_message("指令语法错误", err_msg)
                return False
            if not self.host.workflow_review_controller.confirm_loop_risks(
                tasks,
                transient_cfg,
            ):
                return False
            try:
                run_request = RunRequest.create(
                    tasks,
                    transient_cfg,
                    self.host.current_profile_name,
                    plugin_dependencies=sorted(
                        collect_plugin_dependencies(tasks)
                    ),
                )
                run_request.config.apply_to(self.host.engine)
                prepared_tasks = run_request.mutable_tasks()
            except RunConfigError as error:
                self.host.show_warning_message("错误", str(error))
                return False
            return self._launch_prepared_tasks(
                prepared_tasks,
                transient_cfg,
                f"从第 {selected_index + 1} 步开始执行",
                plugin_dependencies=run_request.plugin_dependencies,
            )
        finally:
            self.start_in_progress = False

    def build_single_step_request(self, task, cfg):
        # MAINTENANCE: When adding new step fields that affect runtime behavior,
        # add them to the reset dict below. Consider moving to a centralized
        # single_step_overrides() in step_runtime.py when the field count grows further.
        single_task = copy.deepcopy(task)
        single_task.update(
            {
                "retry": 1,
                "repeat_mode": "执行一次",
                "repeat_count": "1",
                "no_skip_wait": False,
                "fail_limit": "1",
                "run_max_executions": "1",
                "step_loop_start": "1",
                "step_loop_end": "0",
                "jump_only": False,
                "call_step": "0",
                "call_target_id": "",
                "success_skip": "0",
                "success_jump": "0",
                "success_target_id": "",
                "fail_skip": "0",
                "fail_jump": "0",
                "fail_target_id": "",
                "until_false_jump": "0",
                "until_false_target_id": "",
                "until_true_jump": "0",
                "until_true_target_id": "",
                "until_max_checks": "1",
                "until_on_limit": "继续下一步",
                "debug_breakpoint": False,
            }
        )
        single_cfg = copy.deepcopy(cfg)
        single_cfg.update(
            {
                "loop_mode": "单次",
                "loop_val": "1",
                "start_step": "1",
                "loop_start_round": "1",
                "loop_end_round": "0",
            }
        )
        return [single_task], single_cfg

    def run_selected_step_once(self):
        if not self.host.runtime_ready:
            self.host.show_single_step_tooltip("识别运行库仍在准备，请稍候")
            return
        # ADV-20261006-002: 与手动运行共用同一判据。
        if self.is_launch_blocked():
            reason = self.launch_blocked_reason()
            if reason:
                self.host.show_single_step_tooltip(reason)
            return
        selected_index = self.host.selected_row_index()
        if selected_index is None:
            self.host.show_information_message("执行一步", "请先在步骤列表中选中一个步骤。")
            return
        self.start_in_progress = True
        try:
            cfg = self.host.get_current_ui_config()
            tasks = cfg.get("tasks", [])
            if not 0 <= selected_index < len(tasks):
                return
            if task_is_disabled(tasks[selected_index]):
                self.host.show_information_message(
                    "步骤已禁用",
                    "当前步骤已被暂时禁用。请在小齿轮中关闭“暂时禁用本步骤”后再执行。",
                )
                return
            single_tasks, single_cfg = self.host.build_single_step_request(
                tasks[selected_index], cfg
            )
            error_message = self.host.workflow_review_controller.validate_tasks(
                single_tasks, single_cfg
            )
            if error_message:
                self.host.show_critical_message("当前步骤无法执行", error_message)
                return
            try:
                run_request = RunRequest.create(
                    single_tasks,
                    single_cfg,
                    f"{self.host.current_profile_name} / 第{selected_index + 1}步",
                )
                run_request.config.apply_to(self.host.engine)
                prepared_tasks = run_request.mutable_tasks()
            except RunConfigError as error:
                self.host.show_warning_message("错误", str(error))
                return
            command_name = self.host.engine.get_cmd_name(single_tasks[0].get("type"))
            # FR-20260811-D10: 直接调自身方法，消除 controller→host(forwarder)→
            # controller 的冗余回环（forwarders/execution.py 为纯透传）。
            self._launch_prepared_tasks(
                prepared_tasks,
                single_cfg,
                f"单步执行：原第 {selected_index + 1} 步 {command_name}",
            )
        finally:
            self.start_in_progress = False

    def _launch_prepared_tasks(self, tasks, cfg, run_label, run_meta=None, profile_name=None, max_runtime_seconds=None, plugin_dependencies=()):
        source = (run_meta or {}).get("source", "manual")
        # This is the common launch boundary for manual, single-step and
        # scheduled runs. The standalone clicker uses the same input backend,
        # so both directions of the ownership rule must be enforced here.
        if self._auto_clicker_is_running():
            message = "连点器正在运行，脚本本次未启动。请先停止连点器。"
            if source == "scheduled":
                self.host.append_application_log(
                    f"<font color='orange'>[计划任务] {message}</font>", LOG_RUN
                )
            else:
                self.host.show_information_message("无法启动脚本", message)
            return False
        # P1-2: enforce scheduled task max_runtime_seconds as a timeout ceiling
        # on the engine. The isolated subprocess receives `config=cfg` below and
        # honours the cap inside its own process; the in-process Worker reuses
        # this host engine, which was already configured with the *uncapped*
        # cfg at prepare_and_launch_profile (run_request.config.apply_to). If we
        # only rewrite the local cfg, the in-process cap is silently ignored.
        if max_runtime_seconds:
            existing = float(cfg.get("timeout", "0") or "0")
            if existing <= 0 or max_runtime_seconds < existing:
                cfg = dict(cfg, timeout=str(max_runtime_seconds))
                # Never block a launch on a config re-apply failure; the
                # isolated path still applies its own copy of cfg.
                with contextlib.suppress(RunConfigError):
                    EngineRunConfig.from_mapping(cfg).apply_to(self.host.engine)
        self.host.ui_performance.reset()
        log_policy = self.host.current_log_policy()
        if log_policy.allows(LOG_RUN):
            start_key = cfg["hotkey_start"]
            stop_key = cfg["hotkey_stop"]
            multi_info = cfg.get("multi_target_mode", "快速一个")
            if multi_info == "全部匹配":
                multi_info = f"{multi_info}/{cfg.get('multi_target_order', '从上到下')}"
            # REVIEW-20260913-001 FR-002: 此前用裸 int(float(...)) 解析；非法值
            # （"abc"）抛 ValueError、"inf"/"1e999" 抛 OverflowError，都会从启动路径
            # 逃逸。改用共享边界 parse_float_text（拒绝非有限值，失败回退默认值）。
            start_step = int(parse_float_text(cfg.get("start_step", "1"), 1.0))
            initial_start_raw = cfg.get("initial_start_step", "")
            initial_start_step = (
                int(parse_float_text(initial_start_raw, 1.0))
                if initial_start_raw
                else None
            )
            loop_start_round = int(parse_float_text(cfg.get("loop_start_round", "1"), 1.0))
            loop_end_round = int(parse_float_text(cfg.get("loop_end_round", "0"), 0.0))
            loop_range_info = f"循环范围: 第{loop_start_round}次起" + (f" 至第{loop_end_round}次" if loop_end_round > 0 else "")
            timeout_mode = "超时急停" if cfg.get("timeout_stop", False) else "超时按失败处理"
            runner_mode = (
                "独立 Runner（实验）"
                if config_bool(cfg.get("isolated_runner_en", False))
                else "进程内 Runner"
            )
            self.host.append_application_log(
                f"<hr><b><font color='blue'>>>> {run_label} ({start_key}启动 / {stop_key}停止) "
                f"- 当前方案 - 日志: {self.host.current_log_level_text()} "
                f"- 循环: {cfg.get('loop_mode', '单次')} - {loop_range_info} "
                + (
                    f"- 首轮起始步: {initial_start_step} / 后续起始步: {start_step} "
                    if initial_start_step is not None
                    else f"- 起始步: {start_step} "
                )
                + f"- 多目标: {multi_info} "
                f"- {timeout_mode} - {runner_mode}</font></b>",
                LOG_RUN,
            )

        # Acquire plugin run-refs so uninstall is blocked for the run lifetime.
        plugin_deps = tuple(plugin_dependencies)
        registry = getattr(self.host, "plugin_registry", None)
        if registry is not None and plugin_deps:
            registry.acquire_for_run(plugin_deps)
        self._run_plugin_deps = plugin_deps

        try:
            self._enter_running_ui_state(cfg, source=source)
            started, error = self.host.run_controller.start(
                tasks,
                self.host.append_log,
                self.host.update_running_status_overlay,
                self.host.show_click_indicator_overlay,
                self.host.handle_debug_event,
                self.host.show_script_prompt,
                backend_mode=(
                    "isolated_process"
                    if config_bool(cfg.get("isolated_runner_en", False))
                    else "in_process"
                ),
                config=cfg,
                base_dir=self.host.base_dir,
                profile_name=profile_name or self.host.current_profile_name,
                logging_options={
                    "log_to_file": GLOBAL_CONFIG["log_to_file"],
                    "log_to_ui": GLOBAL_CONFIG["log_to_ui"],
                    "privacy_protected": GLOBAL_CONFIG["privacy_protected"],
                },
                run_meta=run_meta,
            )
            if not started:
                raise RuntimeError(error or "执行引擎未能启动")
            return True
        except Exception as e:
            self._release_plugin_run_refs()
            self._restore_idle_ui_state()
            # Risk 2: scheduled runs must not block the UI with a modal dialog.
            if source == "scheduled":
                write_log(
                    f"计划任务触发被跳过：{e}",
                    context=build_run_log_context(run_meta),
                             category=LOG_RUN,
                         )
                return False
            self.host.show_critical_message("启动失败", str(e))
            return False

    def prepare_and_launch_profile(
        self,
        profile_id: str,
        source: str,
        source_id: str,
        run_id: str,
        due_utc,
        max_runtime_seconds=None,
    ) -> bool:
        """Launch a detached profile for schedules or profile hotkeys.

        Resolves a profile by its stable id, builds a detached ``RunRequest``
        tagged with trigger provenance and plugin dependencies, then launches it
        through the same run path used for manual runs. Returns ``True`` only
        if the engine actually started.

        FR-20261006-006: this入口 previously checked only 连点器 (further down, at
        the common launch boundary) and omitted the 识别测试 term entirely. A
        recognition test injects real input, so a schedule or profile hotkey firing
        while one was running gave two input owners at once. It now consults the
        shared :meth:`launch_blocked_reason` so this path cannot drift again.
        """
        if not self.host.runtime_ready:
            return False
        if self.is_launch_blocked():
            reason = self.launch_blocked_reason()
            message = reason or "识别测试或脚本正在运行，本次未启动。"
            self.host.append_application_log(
                f"<font color='orange'>[计划任务] {message}</font>", LOG_RUN
            )
            return False
        self._ensure_watchdog_release_connected()
        model = self.host.profile_model
        name = model.find_by_id(profile_id)
        if not name:
            return False
        cfg = model.get_config_by_id(profile_id)
        if not cfg:
            return False
        tasks = cfg.get("tasks", [])
        if not tasks:
            return False
        # FR-20261008-701: this is the schedules / profile-hotkey launch entry,
        # and it was the only launch path that skipped the simple/advanced view
        # filter. A debug breakpoint saved while in simple mode therefore still
        # fired on an unattended schedule run (and a profile hotkey), unlike the
        # manual path in ``_start_task_impl``. Apply the same filter before
        # validating, collecting plugin dependencies and building the RunRequest
        # so all three act on the filtered task list.
        tasks = self.host.tasks_for_current_settings_mode(tasks)
        # REVIEW-20260913-001 FR-005: 本方法是计划任务与方案快捷键共用的启动入口。
        # 方案快捷键在调用前已自行校验，手动运行在 _start_task_impl 校验，但计划任务
        # 协调器直接调用本方法，此前全程没有启动前步骤校验——无人值守运行因此缺少与
        # 手动运行一致的提前阻断（设计基准「产品使命」第 2 条：执行结果可解释）。
        validation_error = self.host.workflow_review_controller.validate_tasks(tasks, cfg)
        if validation_error:
            write_log(
                f"计划任务启动被拒绝：{validation_error}",
                context=build_run_log_context(
                    {"schedule_id": source_id, "run_id": run_id, "source": source}
                ),
                         category=LOG_RUN,
                         critical=True,
                     )
            return False
        try:
            run_request = RunRequest.create(
                tasks,
                cfg,
                name,
                source=source,
                schedule_id=source_id,
                plugin_dependencies=sorted(collect_plugin_dependencies(tasks)),
            )
            run_request.config.apply_to(self.host.engine)
            prepared_tasks = run_request.mutable_tasks()
        except RunConfigError:
            return False
        run_meta = {
            "schedule_id": source_id,
            "run_id": run_id,
            "source": source,
        }
        run_label = (
            f"快捷键启动方案：{name}"
            if source == "profile_hotkey"
            else f"计划任务执行：{name}"
        )
        return self._launch_prepared_tasks(
            prepared_tasks,
            cfg,
            run_label,
            run_meta=run_meta,
            profile_name=name,
            max_runtime_seconds=max_runtime_seconds,
            plugin_dependencies=run_request.plugin_dependencies,
        )

    def scheduled_health(self, profile_id: str):
        """Return (profile_exists, config_valid, plugin_ok) for scheduled task pre-check.

        Called by ScheduledTaskService._health to drive the PROFILE_MISSING /
        INVALID_CONFIG / PLUGIN_OR_CAPABILITY_MISSING status transitions
        (design §4.3, ADR 0035).
        """
        model = getattr(self.host, "profile_model", None)
        if model is None:
            return (True, True, True)
        profile_exists = model.find_by_id(profile_id) is not None
        # Config validity: profile existing implies its config is loadable
        # (get_config_by_id returns a deep copy or None; existence is sufficient).
        config_valid = profile_exists
        plugin_ok = self._check_plugin_dependencies_installed(profile_id, model)
        return (profile_exists, config_valid, plugin_ok)

    def _check_plugin_dependencies_installed(self, profile_id, model):
        """Verify every plugin dependency declared by the profile's tasks.

        Drives the PLUGIN_OR_CAPABILITY_MISSING branch (design §4.3 / ADR 0035).
        Returns True when the profile needs no plugin or every required plugin is
        installed and enabled. Returns True (permissive) only when the plugin
        subsystem is unavailable so a missing registry cannot itself fail a
        scheduled run; plugin failures are still caught at launch time by
        prepare_and_launch_profile -> RunRequest.create.
        """
        registry = getattr(self.host, "plugin_registry", None)
        if registry is None:
            return True
        cfg = model.get_config_by_id(profile_id)
        if not cfg:
            return True
        tasks = cfg.get("tasks", [])
        if not tasks:
            return True
        dependencies = collect_plugin_dependencies(tasks)
        if not dependencies:
            return True
        for dependency in dependencies:
            try:
                status = registry.status(dependency)
            except PluginRegistryError:
                return False
            if status != PluginStatus.ENABLED:
                return False
        return True

    def _enter_running_ui_state(self, cfg, source="manual"):
        """Set UI widgets to the running state."""
        self.set_runtime_editing_locked(True)
        self.host.set_start_enabled(False)
        self.host.set_single_step_enabled(False)
        self.host.set_stop_enabled(True)
        self.host.clear_debug_pause_highlight()
        self.host.set_debug_pause_enabled(
            self.host.current_settings_mode() == "advanced"
        )
        self.host.set_debug_continue_enabled(False)
        self.host.set_debug_next_enabled(False)
        self.host.set_debug_variables_enabled(False)
        if self.host.is_mini_checked():
            self.host.showMinimized()

        if cfg.get("run_status_tip", True):
            self.host.show_running_status_overlay(cfg.get("run_status_pos", "右上角"))
        else:
            self.host.hide_running_status_overlay()

        # Risk 3: only show the modal emergency-stop tip for manual runs;
        # hotkey and scheduled triggers must not be blocked by a dialog.
        if source == "manual":
            self.host.maybe_show_emergency_stop_tip_once()

    def _release_plugin_run_refs(self):
        """Release plugin run-refs acquired for the current/failed run."""
        deps = self._run_plugin_deps
        self._run_plugin_deps = ()
        if not deps:
            return
        registry = getattr(self.host, "plugin_registry", None)
        if registry is not None:
            registry.release_after_run(deps)

    def _on_watchdog_force_release(self, _schedule_id, _run_id):
        """SAFETY: release plugin refs when the watchdog force-releases a hung run.

        The normal release path (on_finish) depends on the worker's finished
        signal, which never fires for a deadlocked worker. Without this handler
        the acquire_for_run ref-count leaks permanently, blocking plugin
        uninstall for the rest of the application lifetime.
        """
        self._release_plugin_run_refs()

    def _ensure_watchdog_release_connected(self):
        """Lazily connect the coordinator watchdog_timeout signal.

        ExecutionController is instantiated before scheduled_service exists on
        the host, so the connection cannot be made in __init__. This is called
        from prepare_and_launch_profile (the scheduled-run entry point) which is
        guaranteed to run only after full initialization.
        """
        if self._watchdog_release_connected:
            return
        service = getattr(self.host, "scheduled_service", None)
        if service is None:
            return
        coordinator = getattr(service, "_coordinator", None)
        if coordinator is None:
            return
        coordinator.watchdog_timeout.connect(self._on_watchdog_force_release)
        self._watchdog_release_connected = True

    def _restore_idle_ui_state(self):
        """Restore UI widgets to the idle state after a failed launch."""
        self.set_runtime_editing_locked(False)
        self.host.set_start_enabled(True)
        self.host.set_single_step_enabled(True)
        self.host.set_stop_enabled(False)
        self.host.set_debug_pause_enabled(False)
        self.host.set_debug_continue_enabled(False)
        self.host.set_debug_next_enabled(False)
        self.host.set_debug_variables_enabled(False)
        self.host.clear_debug_pause_highlight()
        self.host.hide_running_status_overlay()

    def stop_task(self):
        if not self.host.run_controller.is_active:
            return
        self.host.run_controller.stop()
        self.host.cancel_active_script_prompt()
        self.host.set_stop_enabled(False)
        self.host.set_debug_pause_enabled(False)
        self.host.set_debug_continue_enabled(False)
        self.host.set_debug_next_enabled(False)
        self.host.set_debug_variables_enabled(False)

    def on_worker_error(self, message):
        self.host.profile_hotkey_controller.on_run_finished()
        self.host.cancel_active_script_prompt()
        self.host.append_application_log(
            "<font color='red'><b>执行线程异常（详细内容未写入日志）</b></font>",
            LOG_CRITICAL,
            critical=True,
        )
        self.host.show_critical_message("执行线程异常", str(message))

    def on_finish(self):
        self.host.profile_hotkey_controller.on_run_finished()
        self._release_plugin_run_refs()
        self.host.cancel_active_script_prompt()
        self.set_runtime_editing_locked(False)
        self.host.set_start_enabled(True)
        self.host.set_single_step_enabled(True)
        self.host.set_stop_enabled(False)
        self.host.set_debug_pause_enabled(False)
        self.host.set_debug_continue_enabled(False)
        self.host.set_debug_next_enabled(False)
        self.host.set_debug_variables_enabled(False)
        self.debug_variable_values = {}
        self.host.clear_debug_pause_highlight()
        self.host.hide_running_status_overlay()
        self.host.refresh_scale_memory_status()
        if self.host.close_after_worker():
            schedule_widget_callback(self.host, 0, lambda owner: owner.close())
            return
        self.host.append_application_log(
            "<b><font color='blue'>引擎运行结束</font></b>", LOG_RUN
        )
