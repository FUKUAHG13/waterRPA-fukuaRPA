# pyright: reportAttributeAccessIssue=false
"""Mapping-domain compatibility forwarders extracted from RPAWindow (batch 1).

All methods delegate unconditionally to ``self.mapping_controller``.  No logic,
no state — pure call-site preservation for external consumers and Qt signal
slots that reference ``RPAWindow.<method>``.
"""


class MappingForwarderMixin:
    """Thin forwarders for MappingController (45 methods)."""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mapping_backend(self):
        return self.mapping_controller.backend

    @mapping_backend.setter
    def mapping_backend(self, value):
        self.mapping_controller.backend = value

    @property
    def mapping_pickers(self):
        return self.mapping_controller.pickers

    @property
    def mapping_inspector_dialogs(self):
        return self.mapping_controller.inspectors

    @property
    def _bulk_mapping_update(self):
        return self.mapping_controller.bulk_update

    @_bulk_mapping_update.setter
    def _bulk_mapping_update(self, value):
        self.mapping_controller.bulk_update = bool(value)

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def add_key_mapping_row(self, *args, **kwargs):
        return self.mapping_controller.add_key_mapping_row(*args, **kwargs)

    def apply_key_mapping_row_data(self, *args, **kwargs):
        return self.mapping_controller.apply_key_mapping_row_data(*args, **kwargs)

    def clear_key_mapping_rows(self, *args, **kwargs):
        return self.mapping_controller.clear_key_mapping_rows(*args, **kwargs)

    def refresh_mapping_row_labels(self, *args, **kwargs):
        return self.mapping_controller.refresh_mapping_row_labels(*args, **kwargs)

    def mapping_row_index(self, *args, **kwargs):
        return self.mapping_controller.mapping_row_index(*args, **kwargs)

    def remove_key_mapping_row(self, *args, **kwargs):
        return self.mapping_controller.remove_key_mapping_row(*args, **kwargs)

    def get_key_mappings_config(self, *args, **kwargs):
        return self.mapping_controller.get_key_mappings_config(*args, **kwargs)

    def apply_key_mappings_config(self, *args, **kwargs):
        return self.mapping_controller.apply_key_mappings_config(*args, **kwargs)

    # ------------------------------------------------------------------
    # Settings interaction
    # ------------------------------------------------------------------

    def settings_interaction_parent(self, *args, **kwargs):
        return self.mapping_controller.settings_interaction_parent(*args, **kwargs)

    def restore_settings_dialog_focus(self, *args, **kwargs):
        return self.mapping_controller.restore_settings_dialog_focus(*args, **kwargs)

    # ------------------------------------------------------------------
    # Hotkey capture
    # ------------------------------------------------------------------

    def capture_hotkey_text(self, *args, **kwargs):
        return self.mapping_controller.capture_hotkey_text(*args, **kwargs)

    def capture_start_hotkey(self, *args, **kwargs):
        return self.mapping_controller.capture_start_hotkey(*args, **kwargs)

    def capture_stop_hotkey(self, *args, **kwargs):
        return self.mapping_controller.capture_stop_hotkey(*args, **kwargs)

    def capture_mapping_hotkey(self, *args, **kwargs):
        return self.mapping_controller.capture_mapping_hotkey(*args, **kwargs)

    def capture_mapping_hotkey_by_row(self, *args, **kwargs):
        return self.mapping_controller.capture_mapping_hotkey_by_row(*args, **kwargs)

    # ------------------------------------------------------------------
    # Coordinate picking
    # ------------------------------------------------------------------

    def start_mapping_coordinate_pick(self, *args, **kwargs):
        return self.mapping_controller.start_mapping_coordinate_pick(*args, **kwargs)

    def start_mapping_coordinate_pick_by_row(self, *args, **kwargs):
        return self.mapping_controller.start_mapping_coordinate_pick_by_row(*args, **kwargs)

    def on_mapping_coordinate_picked(self, *args, **kwargs):
        return self.mapping_controller.on_mapping_coordinate_picked(*args, **kwargs)

    def on_mapping_coordinate_picked_by_row(self, *args, **kwargs):
        return self.mapping_controller.on_mapping_coordinate_picked_by_row(*args, **kwargs)

    def store_mapping_picker(self, *args, **kwargs):
        return self.mapping_controller.store_mapping_picker(*args, **kwargs)

    # ------------------------------------------------------------------
    # Window binding
    # ------------------------------------------------------------------

    def clear_mapping_window_binding(self, *args, **kwargs):
        return self.mapping_controller.clear_mapping_window_binding(*args, **kwargs)

    def update_mapping_window_binding_ui(self, *args, **kwargs):
        return self.mapping_controller.update_mapping_window_binding_ui(*args, **kwargs)

    def show_mapping_window_inspector(self, *args, **kwargs):
        return self.mapping_controller.show_mapping_window_inspector(*args, **kwargs)

    def refresh_mapping_binding_controls(self, *args, **kwargs):
        return self.mapping_controller.refresh_mapping_binding_controls(*args, **kwargs)

    def ensure_mapping_backend(self, *args, **kwargs):
        return self.mapping_controller.ensure_mapping_backend(*args, **kwargs)

    def hwnd_belongs_to_current_process(self, *args, **kwargs):
        return self.mapping_controller.hwnd_belongs_to_current_process(*args, **kwargs)

    def hwnd_screen_area_at_point(self, *args, **kwargs):
        return self.mapping_controller.hwnd_screen_area_at_point(*args, **kwargs)

    def top_level_window_at_point(self, *args, **kwargs):
        return self.mapping_controller.top_level_window_at_point(*args, **kwargs)

    def bind_mapping_window_by_row(self, *args, **kwargs):
        return self.mapping_controller.bind_mapping_window_by_row(*args, **kwargs)

    def on_mapping_window_picked_by_row(self, *args, **kwargs):
        return self.mapping_controller.on_mapping_window_picked_by_row(*args, **kwargs)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def active_key_mappings(self, *args, **kwargs):
        return self.mapping_controller.active_key_mappings(*args, **kwargs)

    def execute_key_mapping_by_hotkey(self, *args, **kwargs):
        return self.mapping_controller.execute_key_mapping_by_hotkey(*args, **kwargs)

    def current_mapping_click_mode(self, *args, **kwargs):
        return self.mapping_controller.current_mapping_click_mode(*args, **kwargs)

    def perform_mapping_mouse_click(self, *args, **kwargs):
        return self.mapping_controller.perform_mapping_mouse_click(*args, **kwargs)

    def background_click_target_hwnd(self, *args, **kwargs):
        return self.mapping_controller.background_click_target_hwnd(*args, **kwargs)

    def window_matches_binding(self, *args, **kwargs):
        return self.mapping_controller.window_matches_binding(*args, **kwargs)

    def perform_mapping_background_click(self, *args, **kwargs):
        return self.mapping_controller.perform_mapping_background_click(*args, **kwargs)

    def perform_key_mapping_click(self, *args, **kwargs):
        return self.mapping_controller.perform_key_mapping_click(*args, **kwargs)

    def execute_key_mapping(self, *args, **kwargs):
        return self.mapping_controller.execute_key_mapping(*args, **kwargs)
