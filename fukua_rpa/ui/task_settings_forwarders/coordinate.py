# pyright: reportAttributeAccessIssue=false
"""Coordinate-settings compatibility forwarders for TaskConfigDialog (batch 1).

All methods delegate unconditionally to ``self.coordinate_settings_controller``.
No logic, no state — pure call-site preservation for signal slots and callers.
"""


class CoordinateSettingsForwarderMixin:
    """Thin forwarders for CoordinateSettingsController (21 methods)."""

    def update_coord_step_ui(self, _=None):
        return self.coordinate_settings_controller.update_step_ui(_)

    def update_coord_sequence_ui(self, _=None):
        return self.coordinate_settings_controller.update_sequence_ui(_)

    def coord_sequence_points(self):
        return self.coordinate_settings_controller.sequence_points()

    def append_coord_sequence_point(self, value):
        return self.coordinate_settings_controller.append_sequence_point(value)

    def start_coord_sequence_pick(self):
        return self.coordinate_settings_controller.start_sequence_pick()

    def on_coord_sequence_pick_finished(self, points):
        return self.coordinate_settings_controller.on_sequence_pick_finished(points)

    def preview_coord_sequence(self):
        return self.coordinate_settings_controller.preview_sequence()

    def coord_step_manual_active(self):
        return self.coordinate_settings_controller.manual_active()

    def manual_point_indices(self):
        return self.coordinate_settings_controller.manual_point_indices()

    def update_coord_step_manual_ui(self):
        return self.coordinate_settings_controller.update_manual_ui()

    def current_coord_step_points(self):
        return self.coordinate_settings_controller.current_points()

    def refresh_coord_step_preview_points(self):
        return self.coordinate_settings_controller.refresh_preview_points()

    def clear_coord_step_manual_points(self, silent=False):
        return self.coordinate_settings_controller.clear_manual_points(silent)

    def on_coord_step_count_changed(self, text):
        return self.coordinate_settings_controller.on_count_changed(text)

    def current_coord_step_options(self):
        return self.coordinate_settings_controller.current_options()

    def start_coord_step_point_pick(self):
        return self.coordinate_settings_controller.start_point_pick()

    def on_coord_step_point_picked(self, value):
        return self.coordinate_settings_controller.on_point_picked(value)

    def on_coord_step_preview_point_moved(self, index, x, y):
        return self.coordinate_settings_controller.on_preview_point_moved(index, x, y)

    def show_coord_step_preview(self):
        return self.coordinate_settings_controller.show_preview()

    def clear_coord_step_preview(self, *_):
        return self.coordinate_settings_controller.clear_preview(*_)

    def close_coord_step_preview(self):
        return self.coordinate_settings_controller.close_preview()
