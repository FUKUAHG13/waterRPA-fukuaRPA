# pyright: reportAttributeAccessIssue=false
"""Image-settings compatibility forwarders for TaskConfigDialog (batch 2).

All methods delegate unconditionally to ``self.image_settings_controller``.
No logic, no state — pure call-site preservation for signal slots and callers.
"""


class ImageSettingsForwarderMixin:
    """Thin forwarders for ImageSettingsController (13 methods)."""

    def update_image_settings_enabled(self):
        return self.image_settings_controller.update_settings_enabled()

    def update_point_limit_ui(self, _=None):
        return self.image_settings_controller.update_point_limit_ui(_)

    def update_image_click_point_ui(self, _=None):
        return self.image_settings_controller.update_click_point_ui(_)

    def update_step_region_ui(self, _=None):
        return self.image_settings_controller.update_step_region_ui(_)

    def update_template_mask_ui(self):
        return self.image_settings_controller.update_template_mask_ui()

    def edit_template_mask_regions(self):
        return self.image_settings_controller.edit_template_mask_regions()

    def clear_template_mask_regions(self):
        return self.image_settings_controller.clear_template_mask_regions()

    def crop_template_image(self):
        return self.image_settings_controller.crop_template_image()

    def start_step_region_pick(self):
        return self.image_settings_controller.start_step_region_pick()

    def on_step_region_selected(self, rect):
        return self.image_settings_controller.on_step_region_selected(rect)

    def clear_step_region(self):
        return self.image_settings_controller.clear_step_region()

    def select_image_click_point(self):
        return self.image_settings_controller.select_click_point()

    def preview_image_click_point(self):
        return self.image_settings_controller.preview_click_point()
