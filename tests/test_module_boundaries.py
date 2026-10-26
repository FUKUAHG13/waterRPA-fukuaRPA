import ast
import importlib
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.config_store import atomic_write_json, load_profiles_backup
from fukua_rpa.constants import (
    APP_VERSION,
    BUILD_NAME,
    LEGACY_PROFILE_BACKUP_FORMAT,
    PRODUCT_NAME,
    PROFILE_BACKUP_FORMAT,
    PROFILE_SCHEMA_VERSION,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.logging_service import current_log_path, set_log_base_dir
from fukua_rpa.win32_api import (
    hotkey_text_from_pressed_vks,
    parse_hotkey_text,
    pressed_hotkey_display_text,
)


class ModuleBoundaryTests(unittest.TestCase):
    def test_application_source_does_not_import_network_clients(self):
        root = Path(__file__).resolve().parents[1]
        source_paths = [root / "fukuaRPA.py", *(root / "fukua_rpa").rglob("*.py")]
        forbidden = {
            "aiohttp",
            "ftplib",
            "http",
            "httpx",
            "requests",
            "smtplib",
            "socket",
            "telnetlib",
            "urllib",
            "websockets",
        }
        violations = []

        for path in source_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported = []
                imported_names = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module]
                    imported_names = [alias.name for alias in node.names]
                elif isinstance(node, ast.Call):
                    func = node.func
                    # importlib.import_module("xxx")
                    if (
                        (isinstance(func, ast.Attribute) and func.attr == "import_module")
                        or isinstance(func, ast.Name) and func.id == "__import__"
                    ) and node.args and isinstance(node.args[0], ast.Constant):
                        imported = [node.args[0].value]
                for module_name in imported:
                    top_level = module_name.split(".", 1)[0]
                    if (
                        top_level in forbidden
                        or module_name.startswith("PySide6.QtNetwork")
                        or (top_level == "PySide6" and "QtNetwork" in imported_names)
                    ):
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno} {module_name}"
                        )

        self.assertEqual(
            violations,
            [],
            "应用源码不得直接引入外部网络客户端；外部链接只能交给系统浏览器："
            + "; ".join(violations),
        )

    def test_all_architecture_modules_import_without_cycles(self):
        names = [
            "fukua_rpa.constants",
            "fukua_rpa.commands",
            "fukua_rpa.paths",
            "fukua_rpa.config_schema",
            "fukua_rpa.config_store",
            "fukua_rpa.profile_model",
            "fukua_rpa.profile_package",
            "fukua_rpa.workflow_security",
            "fukua_rpa.privacy",
            "fukua_rpa.script_prompt",
            "fukua_rpa.preview_model",
            "fukua_rpa.mapping_backend",
            "fukua_rpa.vision.opencv_runtime",
            "fukua_rpa.pyautogui_runtime",
            "fukua_rpa.performance",
            "fukua_rpa.vision.scale_memory",
            "fukua_rpa.debug_session",
            "fukua_rpa.expressions",
            "fukua_rpa.integrity",
            "fukua_rpa.uia_backend",
            "fukua_rpa.uia_smoke",
            "fukua_rpa.native_smoke",
            "fukua_rpa.window_diagnostics",
            "fukua_rpa.runtime_state",
            "fukua_rpa.runtime_trace",
            "fukua_rpa.run_config",
            "fukua_rpa.runner.protocol",
            "fukua_rpa.runner.backend",
            "fukua_rpa.runner.isolated",
            "fukua_rpa.runner.process",
            "fukua_rpa.scheduler",
            "fukua_rpa.session",
            "fukua_rpa.task_model",
            "fukua_rpa.validation",
            "fukua_rpa.workflow_analysis",
            "fukua_rpa.win32_api",
            "fukua_rpa.coordinates",
            "fukua_rpa.vision.core",
            "fukua_rpa.engine",
            "fukua_rpa.engine.actions_exec",
            "fukua_rpa.engine.conditions",
            "fukua_rpa.engine.coordinates",
            "fukua_rpa.engine.expressions",
            "fukua_rpa.engine.vision_exec",
            "fukua_rpa.worker",
            "fukua_rpa.ui.components",
            "fukua_rpa.ui.performance_hooks",
            "fukua_rpa.ui.input_tools",
            "fukua_rpa.ui.overlays",
            "fukua_rpa.ui.startup",
            "fukua_rpa.ui.settings_view",
            "fukua_rpa.ui.settings_binding",
            "fukua_rpa.ui.settings_sections.automation",
            "fukua_rpa.ui.settings_sections.mapping",
            "fukua_rpa.ui.settings_sections.recognition",
            "fukua_rpa.ui.settings_sections.system",
            "fukua_rpa.ui.task_list",
            "fukua_rpa.ui.task_list_mixins.drag_gesture",
            "fukua_rpa.ui.task_list_mixins.virtualization",
            "fukua_rpa.ui.task_row",
            "fukua_rpa.ui.task_row_binding",
            "fukua_rpa.ui.task_row_presentation",
            "fukua_rpa.ui.task_row_view",
            "fukua_rpa.ui.task_row_mixins.config_capability",
            "fukua_rpa.ui.task_row_mixins.edit_history",
            "fukua_rpa.ui.task_row_mixins.picker",
            "fukua_rpa.ui.task_row_mixins.presentation",
            "fukua_rpa.ui.task_row_controllers.config_dialog_controller",
            "fukua_rpa.ui.task_row_controllers.template_capture_controller",
            "fukua_rpa.ui.image_click_point_dialog",
            "fukua_rpa.ui.task_settings_dialog",
            "fukua_rpa.ui.task_settings_binding",
            "fukua_rpa.ui.task_settings_controllers.condition_controller",
            "fukua_rpa.ui.task_settings_controllers.coordinate_controller",
            "fukua_rpa.ui.task_settings_controllers.debug_controller",
            "fukua_rpa.ui.task_settings_controllers.image_controller",
            "fukua_rpa.ui.task_settings_sections.conditions",
            "fukua_rpa.ui.task_settings_sections.coordinates",
            "fukua_rpa.ui.task_settings_sections.debug",
            "fukua_rpa.ui.task_settings_sections.execution",
            "fukua_rpa.ui.task_settings_sections.image",
            "fukua_rpa.ui.controllers.diagnostics_controller",
            "fukua_rpa.ui.controllers.execution_controller",
            "fukua_rpa.ui.controllers.feedback_controller",
            "fukua_rpa.ui.controllers.hotkey_controller",
            "fukua_rpa.ui.controllers.log_controller",
            "fukua_rpa.ui.controllers.mapping_controller",
            "fukua_rpa.ui.controllers.profile_controller",
            "fukua_rpa.ui.controllers.recording_controller",
            "fukua_rpa.ui.controllers.run_controller",
            "fukua_rpa.ui.controllers.runtime_init_controller",
            "fukua_rpa.ui.controllers.settings_controller",
            "fukua_rpa.ui.controllers.shutdown_controller",
            "fukua_rpa.ui.controllers.workflow_editor",
            "fukua_rpa.ui.controllers.workflow_review_controller",
            "fukua_rpa.ui.controllers.workflow_transfer_controller",
            "fukua_rpa.ui.controllers.window_performance_controller",
            "fukua_rpa.ui.main_window",
        ]
        for name in names:
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))

    def test_ui_composition_modules_do_not_import_main_window(self):
        ui_dir = Path(__file__).resolve().parents[1] / "fukua_rpa" / "ui"
        candidates = list((ui_dir / "controllers").glob("*.py"))
        candidates.extend((ui_dir / "settings_sections").glob("*.py"))
        candidates.extend((ui_dir / "task_settings_sections").glob("*.py"))
        candidates.extend((ui_dir / "task_settings_controllers").glob("*.py"))
        candidates.extend((ui_dir / "task_row_controllers").glob("*.py"))
        candidates.extend((ui_dir / "task_row_mixins").glob("*.py"))
        candidates.extend((ui_dir / "task_list_mixins").glob("*.py"))
        candidates.extend(
            ui_dir / name
            for name in (
                "image_click_point_dialog.py",
                "settings_binding.py",
                "settings_view.py",
                "task_row_binding.py",
                "task_row_presentation.py",
                "task_row_view.py",
                "task_settings_binding.py",
                "task_settings_view.py",
            )
        )
        for path in candidates:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_modules.append(node.module or "")
            with self.subTest(module=path.name):
                self.assertFalse(
                    any(
                        module == "main_window" or module.endswith(".main_window")
                        for module in imported_modules
                    ),
                    f"{path.name} 不得反向导入 main_window",
                )

    def test_product_identity_is_consistent(self):
        self.assertEqual(PRODUCT_NAME, "fukuaRPA")
        self.assertRegex(APP_VERSION, r"^v\d+\.\d+\.\d+$")
        self.assertEqual(BUILD_NAME, f"{PRODUCT_NAME}_{APP_VERSION}")

    def test_main_window_exposes_stable_runtime_initialization_bridge(self):
        from types import SimpleNamespace

        from fukua_rpa.ui.main_window import RPAWindow

        bridge = object()
        host = SimpleNamespace(
            runtime_init_controller=SimpleNamespace(bridge=bridge)
        )
        self.assertIs(RPAWindow.runtime_init_bridge.fget(host), bridge)

    def test_config_store_reads_new_and_legacy_backup_formats(self):
        profile = {"默认方案": {"tasks": [], "key_mappings": []}}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "profiles_backup.json")
            for format_name in (PROFILE_BACKUP_FORMAT, LEGACY_PROFILE_BACKUP_FORMAT):
                atomic_write_json(
                    path,
                    {
                        "format": format_name,
                        "profiles": profile,
                        "current_profile": "默认方案",
                    },
                )
                loaded, current, error = load_profiles_backup(path)
                self.assertEqual(error, "")
                self.assertEqual(loaded["默认方案"]["tasks"], [])
                self.assertEqual(loaded["默认方案"]["key_mappings"], [])
                self.assertEqual(
                    loaded["默认方案"]["_schema_version"], PROFILE_SCHEMA_VERSION
                )
                self.assertEqual(current, "默认方案")

    def test_engine_uses_its_injected_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = RPAEngine(base_dir=temp_dir)
            self.assertEqual(engine.base_dir, os.path.abspath(temp_dir))
            self.assertFalse(engine.native_core.available)
            self.assertIn("fukua_rpa_core.dll", engine.native_core.load_error)

    def test_log_path_follows_injected_runtime_directory(self):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                set_log_base_dir(temp_dir)
                self.assertEqual(
                    current_log_path(), os.path.join(temp_dir, "rpa_debug_log.txt")
                )
        finally:
            set_log_base_dir(None)

    def test_source_default_crash_log_is_outside_repository_root(self):
        set_log_base_dir(None)
        path = os.path.abspath(current_log_path())
        self.assertTrue(path.startswith(os.path.abspath(tempfile.gettempdir())))
        self.assertIn("fukuaRPA-development", path)

    def test_hotkey_parser_accepts_bare_letter_for_mapping_mode(self):
        parsed = parse_hotkey_text("A")
        self.assertEqual(parsed["text"], "a")
        self.assertTrue(parsed["bare"])

    def test_hook_key_state_builds_all_modifier_combinations(self):
        letter_a = 0x41
        combinations = {
            frozenset((0xA2, letter_a)): "ctrl+a",
            frozenset((0xA4, letter_a)): "alt+a",
            frozenset((0xA2, 0xA0, letter_a)): "ctrl+shift+a",
            frozenset((0xA2, 0xA4, letter_a)): "ctrl+alt+a",
            frozenset((0xA2, 0xA0, 0xA4, letter_a)): "ctrl+alt+shift+a",
        }
        for pressed_vks, expected in combinations.items():
            self.assertEqual(
                hotkey_text_from_pressed_vks(letter_a, pressed_vks), expected
            )

    def test_hook_key_state_can_render_modifier_only_progress(self):
        self.assertEqual(pressed_hotkey_display_text({0xA2}), "Ctrl")
        self.assertEqual(
            pressed_hotkey_display_text({0xA2, 0xA0, 0xA4, 0x41}),
            "Ctrl+Alt+Shift+A",
        )


if __name__ == "__main__":
    unittest.main()
