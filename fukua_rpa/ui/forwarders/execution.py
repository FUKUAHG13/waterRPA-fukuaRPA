# pyright: reportAttributeAccessIssue=false
"""Execution/debug/button-state compatibility forwarders (batch 3).

Delegates to ``self.execution_controller`` for execution lifecycle and debug
methods, and to individual button widgets for enabled-state setters.
"""


class ExecutionForwarderMixin:
    """Thin forwarders for ExecutionController + button state (26 methods)."""

    # ------------------------------------------------------------------
    # Properties (execution_controller)
    # ------------------------------------------------------------------

    @property
    def start_in_progress(self):
        return self.execution_controller.start_in_progress

    @property
    def debug_paused_step(self):
        return self.execution_controller.debug_paused_step

    @property
    def debug_variable_values(self):
        return self.execution_controller.debug_variable_values

    # ------------------------------------------------------------------
    # Debug control
    # ------------------------------------------------------------------

    def clear_debug_pause_highlight(self, *args, **kwargs):
        return self.execution_controller.clear_debug_pause_highlight(*args, **kwargs)

    def show_debug_variables(self, *args, **kwargs):
        return self.execution_controller.show_debug_variables(*args, **kwargs)

    def pause_debug_run(self, *args, **kwargs):
        return self.execution_controller.pause_debug_run(*args, **kwargs)

    def continue_debug_run(self, *args, **kwargs):
        return self.execution_controller.continue_debug_run(*args, **kwargs)

    def step_over_debug_run(self, *args, **kwargs):
        return self.execution_controller.step_over_debug_run(*args, **kwargs)

    def handle_debug_event(self, *args, **kwargs):
        return self.execution_controller.handle_debug_event(*args, **kwargs)

    # ------------------------------------------------------------------
    # Task execution lifecycle
    # ------------------------------------------------------------------

    def start_task(self, *args, **kwargs):
        return self.execution_controller.start_task(*args, **kwargs)

    def _start_task_impl(self, *args, **kwargs):
        return self.execution_controller._start_task_impl(*args, **kwargs)

    def build_single_step_request(self, *args, **kwargs):
        return self.execution_controller.build_single_step_request(*args, **kwargs)

    def run_selected_step_once(self, *args, **kwargs):
        return self.execution_controller.run_selected_step_once(*args, **kwargs)

    def _launch_prepared_tasks(self, *args, **kwargs):
        return self.execution_controller._launch_prepared_tasks(*args, **kwargs)

    def stop_task(self, *args, **kwargs):
        return self.execution_controller.stop_task(*args, **kwargs)

    def on_worker_error(self, *args, **kwargs):
        return self.execution_controller.on_worker_error(*args, **kwargs)

    def on_finish(self, *args, **kwargs):
        return self.execution_controller.on_finish(*args, **kwargs)

    # ------------------------------------------------------------------
    # Button enabled-state setters
    # ------------------------------------------------------------------

    def set_debug_pause_enabled(self, enabled):
        self.debug_pause_btn.setEnabled(bool(enabled))

    def set_debug_continue_enabled(self, enabled):
        self.debug_continue_btn.setEnabled(bool(enabled))

    def set_debug_next_enabled(self, enabled):
        self.debug_next_btn.setEnabled(bool(enabled))

    def set_debug_variables_enabled(self, enabled):
        self.debug_variables_btn.setEnabled(bool(enabled))

    def set_start_enabled(self, enabled):
        self.start_btn.setEnabled(bool(enabled))

    def set_single_step_enabled(self, enabled):
        self.single_step_btn.setEnabled(bool(enabled))

    def set_stop_enabled(self, enabled):
        self.stop_btn.setEnabled(bool(enabled))

    def is_mini_checked(self):
        return bool(self.mini_chk.isChecked())

    def current_log_level_text(self):
        return self.log_level_combo.currentText()
