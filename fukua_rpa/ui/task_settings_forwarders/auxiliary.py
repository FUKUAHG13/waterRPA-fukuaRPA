# pyright: reportAttributeAccessIssue=false
"""Auxiliary-settings compatibility forwarders for TaskConfigDialog (batch 3).

Groups thin delegations to the until-condition, debug-settings, capture and
background-click controllers.  No logic, no state — pure call-site preservation.
"""


class AuxSettingsForwarderMixin:
    """Thin forwarders for until/debug/capture/background-click controllers (20 methods)."""

    # ------------------------------------------------------------------
    # Until-condition controller
    # ------------------------------------------------------------------

    def update_until_condition_ui(self, _=None):
        return self.until_condition_controller.update_ui(_)

    def select_until_condition_image(self, cond_idx):
        return self.until_condition_controller.select_image(cond_idx)

    def start_until_region_pick(self, cond_idx):
        return self.until_condition_controller.start_region_pick(cond_idx)

    def on_until_region_selected(self, cond_idx, rect):
        return self.until_condition_controller.on_region_selected(cond_idx, rect)

    def start_until_sub_region_pick(self, cond_idx):
        return self.until_condition_controller.start_sub_region_pick(cond_idx)

    def select_until_overlay_image(self, cond_idx):
        return self.until_condition_controller.select_overlay_image(cond_idx)

    def start_until_overlay_rect_pick(self, cond_idx):
        return self.until_condition_controller.start_overlay_rect_pick(cond_idx)

    # ------------------------------------------------------------------
    # Debug-settings controller
    # ------------------------------------------------------------------

    def remember_debug_custom_expression(self, text):
        return self.debug_settings_controller.remember_custom_expression(text)

    def on_debug_preset_changed(self, mode):
        return self.debug_settings_controller.on_preset_changed(mode)

    def sync_debug_condition_from_preset(self, _=None):
        return self.debug_settings_controller.sync_condition_from_preset(_)

    def update_debug_preset_ui(self, _=None):
        return self.debug_settings_controller.update_preset_ui(_)

    def update_debug_breakpoint_ui(self, _=None):
        return self.debug_settings_controller.update_breakpoint_ui(_)

    # ------------------------------------------------------------------
    # Capture-settings controller
    # ------------------------------------------------------------------

    def update_capture_region_ui(self, _=None):
        return self.capture_settings_controller.update_ui(_)

    def start_capture_region_pick(self):
        return self.capture_settings_controller.start_region_pick()

    def on_capture_region_selected(self, rect):
        return self.capture_settings_controller.on_region_selected(rect)

    def clear_capture_region(self):
        return self.capture_settings_controller.clear_region()

    # ------------------------------------------------------------------
    # Background-click-settings controller
    # ------------------------------------------------------------------

    def update_background_click_ui(self, _=None):
        return self.background_click_settings_controller.update_ui(_)

    def start_background_window_pick(self):
        return self.background_click_settings_controller.start_window_pick()

    def on_background_window_selected(self, value):
        return self.background_click_settings_controller.on_window_selected(value)

    def clear_background_window_binding(self):
        return self.background_click_settings_controller.clear_binding()
