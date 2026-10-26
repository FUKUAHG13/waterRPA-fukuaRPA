# pyright: reportAttributeAccessIssue=false
"""Settings/performance/hotkey compatibility forwarders (batch 4).

Delegates to ``self.settings_controller``, ``self.window_performance_controller``
and ``self.hotkey_controller``.
"""


class SettingsForwarderMixin:
    """Thin forwarders for settings, performance and hotkey controllers (32 methods)."""

    # ------------------------------------------------------------------
    # Properties (window_performance_controller)
    # ------------------------------------------------------------------

    @property
    def current_process(self):
        return self.window_performance_controller.current_process

    @current_process.setter
    def current_process(self, value):
        self.window_performance_controller.current_process = value

    # ------------------------------------------------------------------
    # Settings mode
    # ------------------------------------------------------------------

    def advanced_settings_are_non_default(self):
        return self.settings_controller.advanced_settings_are_non_default()

    def current_settings_mode(self):
        return self.settings_controller.current_mode()

    def tasks_for_current_settings_mode(self, tasks):
        return self.settings_controller.tasks_for_current_mode(tasks)

    def apply_settings_mode(self, _index=None):
        return self.settings_controller.apply_mode(_index)

    # ------------------------------------------------------------------
    # Credentials / config directory
    # ------------------------------------------------------------------

    def refresh_credential_names(self, selected=""):
        """兼容转发 → settings_controller.refresh_credential_names"""
        return self.settings_controller.refresh_credential_names(selected)

    def edit_credential(self):
        """兼容转发 → settings_controller.edit_credential"""
        return self.settings_controller.edit_credential()

    def delete_credential(self):
        """兼容转发 → settings_controller.delete_credential"""
        return self.settings_controller.delete_credential()

    def open_config_dir(self):
        """兼容转发 → settings_controller.open_config_dir"""
        return self.settings_controller.open_config_dir()

    # ------------------------------------------------------------------
    # UI scale / theme (window_performance_controller)
    # ------------------------------------------------------------------

    def current_cpu_interval(self):
        return self.window_performance_controller.current_cpu_interval()

    def parse_ui_scale_percent(self):
        return self.window_performance_controller.parse_ui_scale_percent()

    def apply_ui_scale_from_edit(self, *_):
        return self.window_performance_controller.apply_ui_scale_from_edit()

    def apply_theme_mode(self, *_):
        return self.window_performance_controller.apply_theme_mode()

    def apply_ui_scale_to_widgets(self, widgets, scale):
        return self.window_performance_controller.apply_ui_scale_to_widgets(
            widgets, scale
        )

    def apply_ui_scale_to_widget(self, widget):
        return self.window_performance_controller.apply_ui_scale_to_widget(widget)

    def on_per_screen_visual_density_changed(self, density):
        controller = getattr(self, "window_performance_controller", None)
        if controller is None:
            return None
        return controller.on_per_screen_visual_density_changed(density)

    def apply_ui_scale(self, scale):
        return self.window_performance_controller.apply_ui_scale(scale)

    # ------------------------------------------------------------------
    # UI config capture / apply
    # ------------------------------------------------------------------

    def get_default_config_dict(self):
        return self.settings_controller.default_config()

    def get_current_ui_config(self):
        return self.settings_controller.capture()

    def apply_ui_config(self, config):
        return self.settings_controller.apply(config)

    def apply_settings_controls_config(self, config):
        """Apply only the settings controls; never rebuilds the step list."""
        return self.settings_controller.apply_controls_only(config)

    # ------------------------------------------------------------------
    # Scale memory
    # ------------------------------------------------------------------

    def current_scale_memory_policy(self):
        """兼容转发 → settings_controller.current_scale_memory_policy"""
        return self.settings_controller.current_scale_memory_policy()

    def refresh_scale_memory_status(self):
        """兼容转发 → settings_controller.refresh_scale_memory_status"""
        return self.settings_controller.refresh_scale_memory_status()

    def clear_scale_memory_session(self):
        """兼容转发 → settings_controller.clear_scale_memory_session"""
        return self.settings_controller.clear_scale_memory_session()

    # ------------------------------------------------------------------
    # Hotkey controller
    # ------------------------------------------------------------------

    def stop_key_mapping_hook(self, *args, **kwargs):
        return self.hotkey_controller.stop_key_mapping_hook(*args, **kwargs)

    def refresh_hotkey_backend(self, *_signal_args, **_signal_kwargs):
        """Qt signals may carry state/text values; the controller needs neither."""

        return self.hotkey_controller.refresh_hotkey_backend()

    def update_hotkeys(self, *args, **kwargs):
        return self.hotkey_controller.update_hotkeys(*args, **kwargs)

    def check_hotkey(self, *args, **kwargs):
        return self.hotkey_controller.check_hotkey(*args, **kwargs)

    # ------------------------------------------------------------------
    # Live window interaction (window_performance_controller)
    # ------------------------------------------------------------------

    def begin_live_window_interaction(self, kind="move_or_resize"):
        return self.window_performance_controller.begin_live_window_interaction(
            kind
        )

    def end_live_window_interaction(self):
        return self.window_performance_controller.end_live_window_interaction()

    def _schedule_live_interaction_idle(self, kind):
        return self.window_performance_controller.schedule_live_interaction_idle(
            kind
        )

    # ------------------------------------------------------------------
    # CPU / minimum width
    # ------------------------------------------------------------------

    def update_cpu_info(self):
        return self.window_performance_controller.update_cpu_info()

    def refresh_main_window_minimum_width(self):
        return self.window_performance_controller.refresh_main_window_minimum_width()
