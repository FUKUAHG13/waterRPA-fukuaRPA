# pyright: reportAttributeAccessIssue=false
"""Profile/log/runtime compatibility forwarders (batch 5).

Delegates to ``self.profile_controller``, ``self.log_controller``,
``self.profile_model``, ``self.runtime_init_controller`` and
``self.diagnostics_controller``.
"""

from ...log_policy import LOG_APPLICATION


class ProfileLogForwarderMixin:
    """Thin forwarders for profile, log, runtime and diagnostics (27 methods)."""

    # ------------------------------------------------------------------
    # Properties (profile_model)
    # ------------------------------------------------------------------

    @property
    def profiles_data(self):
        return self.profile_model.profiles

    @profiles_data.setter
    def profiles_data(self, value):
        self.profile_model.profiles = value

    @property
    def current_profile_name(self):
        return self.profile_model.current_name

    @current_profile_name.setter
    def current_profile_name(self, value):
        self.profile_model.current_name = str(value)

    # ------------------------------------------------------------------
    # Properties (diagnostics / runtime)
    # ------------------------------------------------------------------

    @property
    def integrity_worker(self):
        return self.diagnostics_controller.worker

    @property
    def runtime_ready(self):
        return self.runtime_init_controller.ready

    @property
    def runtime_init_in_progress(self):
        return self.runtime_init_controller.in_progress

    @property
    def runtime_init_bridge(self):
        """Keep the staged-startup contract independent of controller layout."""
        return self.runtime_init_controller.bridge

    # ------------------------------------------------------------------
    # Runtime init
    # ------------------------------------------------------------------

    def initialize_runtime_async(self):
        return self.runtime_init_controller.initialize_async()

    # ------------------------------------------------------------------
    # Log controller
    # ------------------------------------------------------------------

    def append_log(self, msg):
        return self.log_controller.append(msg)

    # FR-20260809-D8-⑤: 由 main_window 内联收拢至本 mixin（与 append_log 同域）。
    def append_application_log(
        self,
        msg,
        category=LOG_APPLICATION,
        *,
        critical=False,
        private_msg=None,
    ):
        return self.log_controller.append_application(
            msg,
            category,
            critical=critical,
            private_message=private_msg,
        )

    def current_log_mode(self):
        return self.log_controller.current_mode()

    def current_custom_log_categories(self):
        return self.log_controller.current_categories()

    def current_log_policy(self):
        return self.log_controller.current_policy()

    def refresh_log_content_button(self):
        return self.log_controller.refresh_content_button()

    def _on_log_context_menu(self, pos):
        """兼容转发 → log_controller.on_log_context_menu"""
        return self.log_controller.on_log_context_menu(pos)

    def warn_heavy_log_level(self, text):
        return self.log_controller.warn_heavy_level(text)

    def on_log_privacy_changed(self, protected):
        return self.log_controller.on_privacy_changed(protected)

    # ------------------------------------------------------------------
    # Profile controller
    # ------------------------------------------------------------------

    def refresh_profile_backup_status(self):
        return self.profile_controller.refresh_backup_status()

    def apply_profile_backup_count(self):
        return self.profile_controller.apply_backup_count()

    def export_profile_backups(self):
        return self.profile_controller.export_backups()

    def restore_profile_backup(self):
        return self.profile_controller.restore_backup()

    def clear_profile_backups(self):
        return self.profile_controller.clear_backups()

    def persist_profiles_state(self, force=False):
        return self.profile_controller.persist_profiles_state(force)

    def mark_profile_dirty(self):
        return self.profile_controller.mark_dirty()

    def profile_has_unsaved_changes(self):
        return self.profile_controller.has_unsaved_changes()

    def autosave_profiles_if_changed(self):
        return self.profile_controller.autosave_profiles_if_changed()

    def on_profile_order_changed(self, names):
        return self.profile_controller.reorder_profiles(names)
