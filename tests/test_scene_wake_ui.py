import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tests.qt_test_support import QtCallbackGuardMixin, create_test_window


class SceneWakeUiTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self, base_dir):
        return create_test_window(self, base_dir)

    def test_scene_wake_settings_round_trip_and_disable_sensitivity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.scene_wake_chk.setChecked(False)
            window.scene_wake_sensitivity_combo.setCurrentIndex(
                window.scene_wake_sensitivity_combo.findData("sensitive")
            )
            config = window.get_current_ui_config()

            self.assertFalse(config["scene_wake_en"])
            self.assertEqual(config["scene_wake_sensitivity"], "sensitive")
            self.assertFalse(window.scene_wake_sensitivity_combo.isEnabled())

            window.scene_wake_chk.setChecked(True)
            window.apply_ui_config(config)

            self.assertFalse(window.scene_wake_chk.isChecked())
            self.assertEqual(
                window.scene_wake_sensitivity_combo.currentData(), "sensitive"
            )
            self.assertFalse(window.scene_wake_sensitivity_combo.isEnabled())


if __name__ == "__main__":
    unittest.main()
