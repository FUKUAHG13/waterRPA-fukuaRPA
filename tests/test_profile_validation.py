import unittest

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.profile_validation import (
    ProfileValidationError,
    validate_profile_for_ui,
)


class ProfileValidationTests(unittest.TestCase):
    def test_default_profile_is_valid_and_detached(self):
        source = default_profile_config()

        prepared = validate_profile_for_ui(source)

        self.assertEqual(prepared["conf"], "0.8")
        prepared["tasks"].append({"type": 5.0, "value": "0"})
        self.assertEqual(source["tasks"], [])

    def test_rejects_invalid_runtime_and_ui_values(self):
        cases = {
            "confidence": ("conf", "not-a-number", "全局相似度"),
            "ui-scale": ("ui_scale", "200", "界面倍率"),
            "theme": ("theme_mode", "unknown", "界面主题"),
            "cpu-refresh": (
                "cpu_refresh_interval",
                1234,
                "资源监测刷新频率",
            ),
            "status-position": (
                "run_status_pos",
                "屏幕中间",
                "提示位置",
            ),
            "mapping-mode": (
                "mapping_click_mode",
                "未知点击",
                "点击方式",
            ),
        }
        for name, (field, value, message) in cases.items():
            profile = default_profile_config()
            profile[field] = value
            with self.subTest(name=name), self.assertRaisesRegex(
                ProfileValidationError, message
            ):
                validate_profile_for_ui(profile)

    def test_rejects_conflicting_control_hotkeys(self):
        profile = default_profile_config()
        profile["hotkey_start"] = "Ctrl+Shift+A"
        profile["hotkey_stop"] = "Ctrl+Shift+A"

        with self.assertRaisesRegex(ProfileValidationError, "不能相同"):
            validate_profile_for_ui(profile)

    def test_rejects_invalid_regions_and_enabled_mapping(self):
        profile = default_profile_config()
        profile["scan_regions"] = [[0, 0, -1, 100]]
        with self.assertRaisesRegex(ProfileValidationError, "宽度和高度"):
            validate_profile_for_ui(profile)

        profile = default_profile_config()
        profile["key_mappings"] = [
            {
                "enabled": True,
                "hotkey": "A",
                "coord": "",
                "action": "左键单击",
                "window_binding": {},
            }
        ]
        profile["key_mapping_count"] = 1
        with self.assertRaisesRegex(ProfileValidationError, "有效热键和坐标"):
            validate_profile_for_ui(profile)


if __name__ == "__main__":
    unittest.main()
