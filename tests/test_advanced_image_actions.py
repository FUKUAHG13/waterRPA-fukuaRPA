import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageOps
from PySide6.QtWidgets import QApplication, QWidget

from fukua_rpa.actions.mouse import _bounded_unit_offset
from fukua_rpa.auxiliary_clicks import (
    AuxiliaryClickRequest,
    AuxiliaryClickScheduler,
)
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import (
    MAX_MULTI_IMAGE_ACTION_RULES,
    TASK_TYPE_MULTI_IMAGE_ACTION,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import StepActionContext
from fukua_rpa.multi_image_actions import normalize_multi_image_rules
from fukua_rpa.profile_package import export_full_package, import_full_package
from fukua_rpa.profile_paths import MANAGED_DATA_PREFIX, resolve_profile_path
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from fukua_rpa.validation import validate_task_list
from fukua_rpa.workflow_security import (
    analyze_workflow_capabilities,
    sanitize_profile_for_sharing,
)
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


def patterned_image(width=12, height=10):
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            (
                (x * 31 + y * 17) % 256,
                (x * 13 + y * 47) % 256,
                (x * 59 + y * 7) % 256,
            )
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


class AuxiliaryClickSchedulerTests(unittest.TestCase):
    def test_scheduler_runs_in_due_order_and_becomes_idle(self):
        completed = []
        scheduler = AuxiliaryClickScheduler(
            lambda request: completed.append(request.kind), max_pending=4
        )
        self.addCleanup(scheduler.close)
        later = AuxiliaryClickRequest("later", 1, 2, "left", "later")
        sooner = AuxiliaryClickRequest("sooner", 3, 4, "left", "sooner")

        self.assertTrue(scheduler.schedule(later, 0.03))
        self.assertTrue(scheduler.schedule(sooner, 0.01))
        self.assertTrue(scheduler.wait_until_idle(poll_seconds=0.005))

        self.assertEqual(completed, ["sooner", "later"])
        self.assertEqual(scheduler.pending_count, 0)

    def test_scheduler_rejects_invalid_delays_and_cancels_pending_work(self):
        completed = []
        scheduler = AuxiliaryClickScheduler(
            lambda request: completed.append(request.kind), max_pending=1
        )
        request = AuxiliaryClickRequest("click", 1, 2, "left", "click")

        self.assertFalse(scheduler.schedule(request, float("nan")))
        self.assertFalse(scheduler.schedule(request, float("inf")))
        self.assertTrue(scheduler.schedule(request, 0.2))
        self.assertFalse(scheduler.schedule(request, 0.2))
        scheduler.close(cancel_pending=True, wait=True)
        time.sleep(0.03)

        self.assertEqual(completed, [])


class DragOffsetParsingTests(unittest.TestCase):
    """REVIEW-20260913-001 FR-002：拖拽起点/终点偏移必须拒绝非有限值。

    该字段此前用裸 ``float()`` 解析，``"inf"``/``"nan"``/``"1e999"`` 会通过，
    随后 ``int()`` 转换抛 ``OverflowError``/``ValueError``，把步骤变成执行崩溃。
    """

    def test_non_finite_offsets_fall_back_to_center(self):
        for raw in ("inf", "-inf", "nan", "1e999", "abc", "", "  "):
            with self.subTest(value=raw):
                self.assertEqual(_bounded_unit_offset(raw), 0.5)

    def test_valid_offsets_are_kept_and_clamped(self):
        self.assertEqual(_bounded_unit_offset("0.25"), 0.25)
        self.assertEqual(_bounded_unit_offset("0"), 0.0)
        self.assertEqual(_bounded_unit_offset("1"), 1.0)
        self.assertEqual(_bounded_unit_offset("-0.5"), 0.0)
        self.assertEqual(_bounded_unit_offset("1.5"), 1.0)


class AdvancedImageActionEngineTests(unittest.TestCase):
    def create_engine(self):
        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        engine.use_native_core = False
        return engine

    def test_inverse_matching_is_opt_in_and_finds_inverted_target(self):
        engine = self.create_engine()
        engine.opencv_available = True
        engine.enable_grayscale = False
        engine.min_scale = 1.0
        engine.max_scale = 1.0
        engine.scale_step = 0.1
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            template = patterned_image()
            template.save(path)
            task = {"type": 1.0, "value": path}
            self.assertTrue(engine.load_and_precompute([task]))
            cache_key = engine.image_target_cache_key(task)
            self.assertNotIn("cache_key", task)

            screenshot = Image.new("RGB", (70, 50), (72, 91, 113))
            screenshot.paste(ImageOps.invert(template), (24, 19))
            ordinary = engine.find_target_in_screenshot(
                path, cache_key, 0.95, False, screenshot, 0, 0
            )
            inverted = engine.find_target_in_screenshot(
                path,
                cache_key,
                0.95,
                False,
                screenshot,
                0,
                0,
                allow_inverted=True,
            )

        self.assertIsNone(ordinary)
        self.assertIsNotNone(inverted)
        self.assertEqual((int(inverted[0]), int(inverted[1])), (30, 24))
        self.assertGreaterEqual(inverted[3], 0.99)

    def test_multi_image_action_uses_first_matching_rule(self):
        engine = self.create_engine()
        engine.find_target_optimized = mock.Mock(
            side_effect=[None, (80.0, 60.0, 1.0, 0.97)]
        )
        engine.perform_mouse_click = mock.Mock(return_value=True)
        context = StepActionContext(
            command=TASK_TYPE_MULTI_IMAGE_ACTION,
            value="",
            retry=1,
            step_info={"step": 2, "loop": 1, "cmd": "多图动作映射"},
            cache_key="",
            confidence=0.8,
            use_gray=True,
            task={
                "multi_image_actions": [
                    {"image": "first.png", "action": "left_click"},
                    {"image": "second.png", "action": "right_click"},
                    {"image": "third.png", "action": "left_click"},
                ]
            },
        )

        result = engine._execute_multi_image_action(context)

        self.assertEqual(result, "success")
        self.assertEqual(engine.find_target_optimized.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in engine.find_target_optimized.call_args_list],
            ["first.png", "second.png"],
        )
        engine.perform_mouse_click.assert_called_once_with(
            80.0, 60.0, 1, "right", "匹配点右键单击"
        )

    def test_inverse_mode_bypasses_only_the_unsupported_native_path(self):
        engine = self.create_engine()
        engine.native_find_targets = mock.Mock(
            return_value=[(40.0, 50.0, 1.0, 0.96)]
        )

        ordinary = engine.find_target_optimized(
            "target.png", "cache", 0.8, True, None, False
        )
        self.assertEqual(ordinary, (40.0, 50.0, 1.0, 0.96))
        engine.native_find_targets.assert_called_once()

        engine.native_find_targets.reset_mock()
        engine.last_target_positions.clear()
        engine.iter_search_screenshots = mock.Mock(return_value=[])
        inverted = engine.find_target_optimized(
            "target.png", "cache", 0.8, True, None, True
        )

        self.assertIsNone(inverted)
        engine.native_find_targets.assert_not_called()

    def test_click_supplements_are_queued_without_blocking_main_action(self):
        engine = self.create_engine()
        scheduler = mock.Mock()
        scheduler.schedule.return_value = True
        engine.auxiliary_click_scheduler = scheduler

        engine.schedule_click_supplements(
            img_path="target.png",
            cache_key="target-cache",
            confidence=0.86,
            use_gray=True,
            allow_inverted=True,
            center=(100, 120),
            scale=1.25,
            click_point=(104, 126),
            button="left",
            binding=None,
            matched_size=(150.0, 60.0),
            config={
                "delayed_enabled": True,
                "delayed_seconds": 0.4,
                "confirm_enabled": True,
                "confirm_seconds": 0.8,
            },
        )

        self.assertEqual(scheduler.schedule.call_count, 2)
        delayed_request, delayed_seconds = scheduler.schedule.call_args_list[0].args
        confirm_request, confirm_seconds = scheduler.schedule.call_args_list[1].args
        self.assertEqual(delayed_request.kind, "delayed_second_click")
        self.assertEqual((delayed_request.x, delayed_request.y), (104.0, 126.0))
        self.assertEqual(delayed_seconds, 0.4)
        self.assertEqual(confirm_request.kind, "image_confirm_click")
        self.assertEqual(confirm_request.verification["scale"], 1.25)
        self.assertEqual(confirm_request.verification["matched_width"], 150.0)
        self.assertEqual(confirm_request.verification["matched_height"], 60.0)
        self.assertTrue(confirm_request.verification["allow_inverted"])
        self.assertEqual(confirm_seconds, 0.8)


class AdvancedImageActionDataTests(unittest.TestCase):
    def test_normalization_is_ordered_detached_and_bounded(self):
        source = [
            {"image": f"{index}.png", "action": "匹配点左键单击"}
            for index in range(MAX_MULTI_IMAGE_ACTION_RULES + 2)
        ]
        normalized = normalize_multi_image_rules(source)
        source[0]["image"] = "changed.png"

        self.assertEqual(len(normalized), MAX_MULTI_IMAGE_ACTION_RULES)
        self.assertEqual(normalized[0]["image"], "0.png")
        self.assertEqual(normalized[0]["action"], "left_click")

    def test_validation_accepts_valid_rules_and_rejects_rule_overflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "rule.png")
            patterned_image().save(image_path)
            rule = {"image": image_path, "action": "left_click"}
            task = {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "value": "",
                "multi_image_actions": [rule],
            }
            self.assertIsNone(validate_task_list([task], default_profile_config()))

            task["multi_image_actions"] = [
                dict(rule) for _index in range(MAX_MULTI_IMAGE_ACTION_RULES + 1)
            ]
            error = validate_task_list([task], default_profile_config())

        self.assertIn("最多允许", error)

    def test_full_package_round_trip_includes_nested_rule_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "nested.png")
            patterned_image().save(image_path)
            profile = default_profile_config()
            profile["tasks"] = [
                {
                    "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                    "value": "",
                    "multi_image_actions": [
                        {"image": image_path, "action": "left_click"}
                    ],
                }
            ]
            package_path = os.path.join(temp_dir, "profile.zip")
            exported = export_full_package(
                profile,
                "多图方案",
                package_path,
                privacy_protected=False,
            )
            receiver = os.path.join(temp_dir, "receiver")
            imported = import_full_package(package_path, receiver)
            rewritten = imported.profile["tasks"][0]["multi_image_actions"][0][
                "image"
            ]

            self.assertEqual(exported.asset_count, 1)
            self.assertTrue(rewritten.startswith(MANAGED_DATA_PREFIX))
            self.assertTrue(os.path.isfile(resolve_profile_path(rewritten, receiver)))

    def test_security_review_counts_and_redacts_nested_rule_capabilities(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "value": "",
                "multi_image_actions": [
                    {"image": "private-a.png", "action": "left_click"},
                    {
                        "image": "private-b.png",
                        "action": "text",
                        "value": "PRIVATE_TEXT",
                    },
                ],
            }
        ]

        report = analyze_workflow_capabilities(profile)
        sanitized = sanitize_profile_for_sharing(profile)

        self.assertEqual(report.mouse_action_count, 1)
        self.assertEqual(report.keyboard_action_count, 1)
        self.assertEqual(report.image_reference_count, 2)
        self.assertEqual(report.dangerous_steps, (1,))
        self.assertEqual(sanitized.removed_image_count, 2)
        self.assertEqual(sanitized.profile["tasks"][0]["multi_image_actions"], [])
        self.assertTrue(sanitized.profile["tasks"][0]["step_disabled"])


class AdvancedImageActionUiTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_dialog(self, temp_dir, data=None, **kwargs):
        dialog = TaskConfigDialog(None, data or {}, base_dir=temp_dir, **kwargs)
        self.addCleanup(dispose_widget, self.app, dialog)
        return dialog

    @staticmethod
    def action_row_is_visible(dialog, row):
        form = dialog.action_row_forms.get(row, dialog.control_form)
        return form.isRowVisible(row)

    def test_new_image_options_are_advanced_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            simple = self.create_dialog(
                temp_dir,
                step_type="左键单击",
                step_value="target.png",
                settings_mode="simple",
            )
            advanced = self.create_dialog(
                temp_dir,
                step_type="左键单击",
                step_value="target.png",
                settings_mode="advanced",
            )

            self.assertFalse(
                self.action_row_is_visible(simple, simple.inverse_match_row)
            )
            self.assertFalse(
                self.action_row_is_visible(simple, simple.image_confirm_click_row)
            )
            self.assertTrue(simple.auxiliary_click_section.isHidden())
            self.assertTrue(
                self.action_row_is_visible(advanced, advanced.inverse_match_row)
            )
            self.assertTrue(
                self.action_row_is_visible(
                    advanced, advanced.image_confirm_click_row
                )
            )
            self.assertFalse(advanced.auxiliary_click_section.isHidden())

    def test_multi_image_rule_editor_is_only_exposed_in_advanced_mode(self):
        data = {
            "multi_image_actions": [
                {"image": "one.png", "action": "left_click"}
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            simple = self.create_dialog(
                temp_dir,
                data,
                step_type="多图动作映射",
                settings_mode="simple",
            )
            advanced = self.create_dialog(
                temp_dir,
                data,
                step_type="多图动作映射",
                settings_mode="advanced",
            )

            self.assertTrue(simple.multi_image_section.isHidden())
            self.assertFalse(advanced.multi_image_section.isHidden())
            self.assertEqual(advanced.multi_image_action_editor.table.rowCount(), 1)

    def test_unknown_child_action_code_survives_editor_roundtrip(self):
        """FR-20260812-D11-002: 未知子动作码不得被改写为 left_click。

        编辑器加载未知码后必须保留原值，rules() 原样输出，由校验层报告
        "命中动作无效"，而不是静默变成可执行的左键单击。
        """
        from fukua_rpa.multi_image_actions import normalize_multi_image_action_code

        unknown = "future_action_xyz"
        data = {
            "multi_image_actions": [
                {"image": "one.png", "action": unknown}
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            dialog = self.create_dialog(
                temp_dir,
                data,
                step_type="多图动作映射",
                settings_mode="advanced",
            )
            editor = dialog.multi_image_action_editor
            self.assertEqual(editor.table.rowCount(), 1)
            combo = editor.table.cellWidget(0, 1)
            self.assertEqual(combo.currentData(), unknown)
            rules = editor.rules()
            self.assertEqual(len(rules), 1)
            # 原样保留未知码，不降级为 left_click
            self.assertEqual(rules[0]["action"], unknown)
            self.assertEqual(
                normalize_multi_image_action_code(unknown), unknown
            )

    def test_accept_closes_the_multi_image_region_picker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dialog = self.create_dialog(
                temp_dir,
                step_type="多图动作映射",
                settings_mode="advanced",
            )
            picker = QWidget()
            self.addCleanup(dispose_widget, self.app, picker)
            picker.show()
            dialog.multi_image_action_editor.region_window = picker

            dialog.accept()
            self.app.processEvents()

            self.assertFalse(picker.isVisible())
            self.assertIsNone(dialog.multi_image_action_editor.region_window)


class MultiImageChildCommandConsistencyTests(unittest.TestCase):
    """Guard against drift between MULTI_IMAGE_CHILD_ACTIONS and the engine's
    child_commands dispatch table (engine_actions.py:1179-1187)."""

    def test_all_non_point_child_actions_have_engine_command_mapping(self):
        from fukua_rpa.multi_image_actions import (
            MULTI_IMAGE_CHILD_ACTIONS,
            MULTI_IMAGE_POINT_ACTIONS,
        )

        # Mirror of the dispatch table in engine_actions._execute_multi_image_action
        child_commands = {
            "text": 4.0,
            "wait": 5.0,
            "scroll": 6.0,
            "hotkey": 7.0,
            "prompt": 12.0,
            "stop": 13.0,
            "sound": 14.0,
        }

        non_point_codes = {
            action.code
            for action in MULTI_IMAGE_CHILD_ACTIONS
            if action.code not in MULTI_IMAGE_POINT_ACTIONS
        }
        missing = non_point_codes - set(child_commands)
        self.assertEqual(
            missing,
            set(),
            f"Non-point child actions missing from child_commands dispatch: {missing}. "
            f"Update engine_actions.py child_commands when adding new sub-actions.",
        )


class MultiImageRuleCacheKeyTests(unittest.TestCase):
    """FR-SCOPE013-006 / FR-SCOPE012-001：多图规则缓存键的一致性与可信度。

    `FR-SCOPE013-006`（前置最小验证）：预计算键（`vision/preload.py`）与运行期键
    （`actions/image.py`）此前是两份独立 f-string，当前恰好一致但**无测试守护**。
    这里断言「运行期交给 `find_target_optimized` 的键 == 预计算注册的键」。

    `FR-SCOPE012-001`（前置最小验证）：规则字典里外部导入/手工编辑留下的过期
    `cache_key` 此前被原样转发，使多倍率搜索静默退化为单倍率。这里断言该字段
    不再被信任。
    """

    def create_engine(self):
        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        engine.use_native_core = False
        engine.opencv_available = True
        return engine

    @staticmethod
    def _task(image_path, **extra):
        task = {
            "type": TASK_TYPE_MULTI_IMAGE_ACTION,
            "value": "",
            "custom_en": True,
            "custom_scale_min": 0.8,
            "custom_scale_max": 1.3,
            "custom_scale_step": 0.1,
            "custom_gray": True,
            "multi_image_actions": [{"image": image_path, "action": "left_click"}],
        }
        task.update(extra)
        return task

    @staticmethod
    def _context(task):
        return StepActionContext(
            command=TASK_TYPE_MULTI_IMAGE_ACTION,
            value="",
            retry=1,
            step_info={"step": 1, "loop": 1, "cmd": "多图动作映射"},
            cache_key="",
            confidence=0.8,
            use_gray=True,
            task=task,
        )

    def _preloaded_key(self, engine, task):
        """跑真实预计算路径，返回该规则实际注册的缓存键。

        刻意**不**经过 `multi_image_rule_cache_key`：注册键必须从预计算自身的
        副作用（`scale_options_cache`）读出，否则断言会退化为"构造函数等于它
        自己"，无法暴露运行期与预计算之间的漂移。
        """
        engine.load_and_precompute([task])
        keys = list(engine.scale_options_cache)
        self.assertEqual(
            len(keys), 1, "本例只预计算一条多图规则，注册键应当唯一"
        )
        return keys[0]

    def test_preload_registers_the_key_built_by_the_shared_constructor(self):
        """FR-SCOPE013-006：预计算注册的键由共用构造函数产出（单一来源）。"""
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "rule.png")
            patterned_image().save(image_path)
            task = self._task(image_path)

            registered = self._preloaded_key(engine, task)

            self.assertEqual(
                registered,
                engine.multi_image_rule_cache_key(
                    image_path, 0.8, 1.3, 0.1, True
                ),
            )
            self.assertIn(registered, engine.scaled_templates_cache)

    def test_runtime_key_equals_preload_registered_key(self):
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "rule.png")
            patterned_image().save(image_path)
            task = self._task(image_path)
            expected = self._preloaded_key(engine, task)

            engine.find_target_optimized = mock.Mock(return_value=None)
            engine._execute_multi_image_action(self._context(task))

            engine.find_target_optimized.assert_called_once()
            self.assertEqual(
                engine.find_target_optimized.call_args.args[1],
                expected,
                "运行期缓存键必须等于预计算注册的键，否则多倍率搜索静默退化为单倍率",
            )

    def test_stale_rule_cache_key_is_not_trusted(self):
        """规则自带的过期 `cache_key` 不得改变实际使用的键（FR-SCOPE012-001）。

        修复前：`actions/image.py` 在 `rule["cache_key"]` 非空时原样转发，过期键
        不在 `scaled_templates_cache` 中，多倍率搜索静默退化为单倍率。
        """
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "rule.png")
            patterned_image().save(image_path)
            task = self._task(image_path)
            expected = self._preloaded_key(engine, task)
            task["multi_image_actions"][0]["cache_key"] = "stale-key-xyz"

            engine.find_target_optimized = mock.Mock(return_value=None)
            engine._execute_multi_image_action(self._context(task))

            used_key = engine.find_target_optimized.call_args.args[1]
            self.assertEqual(used_key, expected)
            self.assertNotEqual(used_key, "stale-key-xyz")

    def test_normalized_rules_still_round_trip_the_legacy_field(self):
        """归一化仍保留旧字段（不改变已保存数据的形状），只是运行期不再采信。"""
        rules = normalize_multi_image_rules(
            [{"image": "a.png", "cache_key": "legacy-key"}]
        )
        self.assertEqual(rules[0]["cache_key"], "legacy-key")


class DragImageCacheKeyTests(unittest.TestCase):
    """FR-20260930-013：拖拽起/终点图片必须与图片点击共用同一模板缓存键体系。

    修复前拖拽使用 `drag_source_*` / `drag_target_*` 自定义键，不在任何
    `scale_options_cache` 登记点的键空间内，导致 `scale_options_for` 每次回退
    全局默认倍率、`scaled_templates_cache` 永不命中：OpenCV 回退路径只保留
    scale 1.0 的基础模板，而原生路径按完整配置范围搜索——同一方案在两种后端下
    结果翻转（非 1.0 倍率的拖拽图片在 OpenCV 下永远识别不到）。
    """

    DRAG_TYPE = 10.0  # 左键拖拽（commands.DRAG_COMMANDS）

    def create_engine(self):
        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        engine.use_native_core = False
        engine.opencv_available = True
        return engine

    def _task(self, source_path, target_path, **extra):
        task = {
            "type": self.DRAG_TYPE,
            "value": "",
            "custom_en": True,
            "custom_scale_min": 0.8,
            "custom_scale_max": 1.3,
            "custom_scale_step": 0.1,
            "custom_gray": True,
            "drag_source_img": source_path,
            "drag_target_img": target_path,
        }
        task.update(extra)
        return task

    def _context(self, task):
        return StepActionContext(
            command=self.DRAG_TYPE,
            value="",
            retry=1,
            step_info={"step": 1, "loop": 1, "cmd": "左键拖拽"},
            cache_key="",
            confidence=0.8,
            use_gray=True,
            task=task,
        )

    def test_preload_registers_drag_keys_in_the_shared_key_space(self):
        """预计算必须为拖拽起/终点图片注册键，且键落在共享键空间内。"""
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "drag_source.png")
            target = os.path.join(temp_dir, "drag_target.png")
            patterned_image().save(source)
            patterned_image().save(target)
            task = self._task(source, target)

            self.assertTrue(engine.load_and_precompute([task]))

            registered = set(engine.scale_options_cache)
            self.assertEqual(
                registered,
                {
                    engine.drag_source_cache_key(task),
                    engine.drag_target_cache_key(task),
                },
                "拖拽起/终点图片必须各自注册一个共享键空间的模板缓存键",
            )
            # 倍率档必须来自 image_target_options（0.8/1.3/0.1），而不是默认回退。
            for key in registered:
                self.assertEqual(engine.scale_options_for(key), (0.8, 1.3, 0.1))
                self.assertIn(key, engine.scaled_templates_cache)

    def test_runtime_key_equals_preload_registered_key(self):
        """运行期交给 `find_target_optimized` 的键 == 预计算注册的键。

        刻意从预计算自身的副作用（`scale_options_cache`）读出注册键，而不是调用
        同一个构造函数，否则断言会退化为"构造函数等于它自己"。
        """
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "drag_source.png")
            target = os.path.join(temp_dir, "drag_target.png")
            patterned_image().save(source)
            patterned_image().save(target)
            task = self._task(source, target)
            engine.load_and_precompute([task])

            engine.find_target_optimized = mock.Mock(return_value=None)
            context = self._context(task)
            engine._resolve_drag_source_image(context)
            source_key = engine.find_target_optimized.call_args.args[1]
            engine._resolve_drag_target_image(context)
            target_key = engine.find_target_optimized.call_args.args[1]

            self.assertIn(
                source_key,
                engine.scale_options_cache,
                "拖拽起点图片的运行期键必须已由预计算注册，否则 OpenCV 只搜 1.0",
            )
            self.assertIn(
                target_key,
                engine.scale_options_cache,
                "拖拽终点图片的运行期键必须已由预计算注册，否则 OpenCV 只搜 1.0",
            )
            self.assertNotEqual(source_key, target_key)
            self.assertEqual(engine.scale_options_for(source_key), (0.8, 1.3, 0.1))

    def test_missing_drag_image_is_skipped_not_raised(self):
        """缺失的拖拽图片不在预计算阶段抛错（由校验与运行期负责报错）。"""
        engine = self.create_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = os.path.join(temp_dir, "nope.png")
            task = self._task(missing, missing)

            self.assertTrue(engine.load_and_precompute([task]))
            self.assertEqual(list(engine.scale_options_cache), [])


if __name__ == "__main__":
    unittest.main()
