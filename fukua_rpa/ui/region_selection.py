# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Region-selection helpers extracted verbatim from RPAWindow (main_window.py).

Each function takes the RPAWindow instance as its first argument and runs the
original method body unchanged; main_window.py delegates to it so the public
API is preserved.
"""

from .lifecycle import clear_attribute_if_same
from .region_window_manager import region_window_manager


def open_region_selector(self):
    region_window_manager.close(self, "region_selector")
    window = region_window_manager.open(
        host=self,
        key="region_selector",
        multi=True,
        purpose="recognition",
        on_selected=self.on_regions_selected,
        multi_select=True,
    )
    self.region_win = window
    window.destroyed.connect(
        lambda *_args, current=window: clear_attribute_if_same(
            self, "region_win", current
        )
    )



def open_multi_region_selector(self):
    self.open_region_selector()



def on_region_selected(self, rect_tuple):
    self.engine.apply_scan_region(rect_tuple)
    self.update_region_label()
    self.mark_profile_dirty()
    self.append_application_log(f"已锁定游戏区域(物理): {rect_tuple} (速度+++)")



def on_regions_selected(self, rects):
    """兼容转发 → settings_controller.on_regions_selected"""
    return self.settings_controller.on_regions_selected(rects)



def normalize_region_list(self, regions):
    """兼容转发 → settings_controller.normalize_region_list"""
    return self.settings_controller.normalize_region_list(regions)



def update_region_label(self):
    """兼容转发 → settings_controller.update_region_label"""
    return self.settings_controller.update_region_label()



def apply_scan_region_config(self, scan_region=None, scan_regions=None):
    """兼容转发 → settings_controller.apply_scan_region_config"""
    return self.settings_controller.apply_scan_region_config(scan_region, scan_regions)
