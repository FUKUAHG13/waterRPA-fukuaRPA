import math
import os
import random
import unittest
from unittest import mock

from fukua_rpa.mapping_backend import (
    _CHILD_ENUM_MAX_CANDIDATES,
    WindowMappingBackend,
)
from fukua_rpa.preview_model import (
    apply_preview_point_position,
    build_coordinate_preview,
    translate_preview_task_positions,
)
from fukua_rpa.task_model import build_coord_step_positions, parse_float_text
from fukua_rpa.win32_api import user32


# TODO(L-54): 本模块覆盖偏薄，待补充边界测试：极端坐标范围、PostMessage 全部失败与
# 部分失败的后台点击语义、绑定解析失败或目标窗口已销毁、进程路径大小/符号链接归一化，
# 以及多显示器缩放下的坐标换算。
class PreviewModelTests(unittest.TestCase):
    @staticmethod
    def _group(*group_ids):
        return [
            {"id": group_id, "name": group_id, "collapsed": False}
            for group_id in group_ids
        ]

    def test_target_path_uses_point_count_and_ignores_directional_distance_limit(self):
        points = build_coord_step_positions(
            0,
            0,
            {
                "direction": "移动到新点位",
                "point": "100,0",
                "max_steps": 5,
                "max_distance": 1,
            },
        )

        self.assertEqual(len(points), 5)
        self.assertEqual(points[-1], (100.0, 0.0))

    def test_disabled_steps_are_omitted_without_renumbering_remaining_labels(self):
        plan = build_coordinate_preview(
            [
                {"type": 1.0, "value": "10,10"},
                {"type": 1.0, "value": "20,20", "step_disabled": True},
                {"type": 1.0, "value": "30,30"},
            ]
        )
        self.assertEqual(plan.points, ((10, 10), (30, 30)))
        self.assertEqual(plan.labels, ("1", "3"))

    def test_offset_points_use_dashed_branches_not_a_solid_chain(self):
        tasks = [
            {"type": 1.0, "value": "0,0"},
            {
                "type": 1.0,
                "value": "10,0",
                "coord_step_en": True,
                "coord_step_direction": "移动到新点位",
                "coord_step_point": "30,0",
                "coord_step_max_steps": "3",
            },
            {"type": 1.0, "value": "40,0"},
        ]
        plan = build_coordinate_preview(tasks)
        self.assertEqual(len(plan.points), 5)
        segments = {(item["from"], item["to"], item["style"]) for item in plan.line_segments}
        self.assertIn((0, 1, "solid"), segments)
        self.assertIn((1, 4, "solid"), segments)
        self.assertIn((0, 2, "dash"), segments)
        self.assertIn((2, 4, "dash"), segments)
        self.assertIn((0, 3, "dash"), segments)
        self.assertIn((3, 4, "dash"), segments)
        self.assertNotIn((1, 2, "solid"), segments)
        self.assertNotIn((2, 3, "solid"), segments)

    def test_drag_has_an_internal_line_but_only_start_is_representative(self):
        plan = build_coordinate_preview(
            [
                {"type": 10.0, "value": "10,20 -> 30,40"},
                {"type": 1.0, "value": "50,60"},
            ]
        )
        segments = {(item["from"], item["to"], item["style"]) for item in plan.line_segments}
        self.assertIn((0, 1, "solid"), segments)
        self.assertIn((0, 2, "solid"), segments)
        self.assertNotIn((1, 2, "solid"), segments)

    def test_random_preview_segments_always_reference_existing_points(self):
        random.seed(20260711)
        for _case in range(200):
            tasks = []
            for index in range(random.randint(1, 100)):
                task = {"type": random.choice([1.0, 8.0, 10.0, 11.0])}
                if task["type"] in (10.0, 11.0):
                    task["value"] = f"{index},{index + 1} -> {index + 2},{index + 3}"
                else:
                    task["value"] = f"{index},{index + 1}"
                tasks.append(task)
            limit = random.randint(1, 80)
            plan = build_coordinate_preview(tasks, max_points=limit)
            self.assertLessEqual(len(plan.points), limit)
            self.assertEqual(len(plan.points), len(plan.labels))
            for segment in plan.line_segments:
                self.assertTrue(0 <= segment["from"] < len(plan.points))
                self.assertTrue(0 <= segment["to"] < len(plan.points))

    def test_empty_task_list_yields_an_empty_preview_plan(self):
        plan = build_coordinate_preview([])
        self.assertEqual(plan.points, ())
        self.assertEqual(plan.labels, ())
        self.assertEqual(plan.line_segments, ())
        self.assertFalse(plan.truncated)

    def test_max_points_zero_or_negative_is_clamped_to_a_single_point(self):
        tasks = [
            {"type": 1.0, "value": "10,10"},
            {"type": 1.0, "value": "20,20"},
        ]
        for limit in (0, -5):
            with self.subTest(max_points=limit):
                plan = build_coordinate_preview(tasks, max_points=limit)
                # max(1, int(limit)) == 1: exactly one point, then truncated.
                self.assertEqual(plan.points, ((10.0, 10.0),))
                self.assertEqual(plan.labels, ("1",))
                self.assertEqual(plan.line_segments, ())
                self.assertTrue(plan.truncated)

    def test_nonfinite_coordinate_step_values_fail_closed(self):
        tasks = [
            {
                "step_id": "distance",
                "type": 1.0,
                "value": "10,20",
                "coord_step_en": True,
                "coord_step_direction": "向下",
                "coord_step_distance": "inf",
                "coord_step_max_steps": "3",
            },
            {
                "step_id": "offset",
                "type": 1.0,
                "value": "30,40",
                "coord_step_en": True,
                "coord_step_direction": "自定义偏移",
                "coord_step_dx": "nan",
                "coord_step_dy": "4",
                "coord_step_max_steps": "2",
            },
            {
                "step_id": "limit",
                "type": 1.0,
                "value": "50,60",
                "coord_step_en": True,
                "coord_step_direction": "向右",
                "coord_step_distance": "5",
                "coord_step_max_distance": "1e309",
                "coord_step_max_steps": "2",
            },
        ]

        self.assertEqual(parse_float_text("inf", 7.0), 7.0)
        self.assertEqual(parse_float_text("nan", 7.0), 7.0)
        plan = build_coordinate_preview(tasks)

        self.assertTrue(plan.points)
        self.assertTrue(
            all(math.isfinite(x) and math.isfinite(y) for x, y in plan.points)
        )
        self.assertEqual(plan.points[0], (10.0, 20.0))

    def test_coordinate_step_positions_reject_nonfinite_base_and_overflow(self):
        self.assertEqual(
            build_coord_step_positions(float("inf"), 10, {}, max_points=5), []
        )
        positions = build_coord_step_positions(
            0,
            0,
            {
                "direction": "向右",
                "distance": "1e308",
                "max_steps": 3,
            },
            max_points=5,
        )
        self.assertTrue(
            all(math.isfinite(x) and math.isfinite(y) for x, y in positions)
        )

    def test_editable_sources_are_limited_to_direct_clicks_and_sequences(self):
        plan = build_coordinate_preview(
            [
                {"step_id": "direct", "type": 1.0, "value": "10,20"},
                {
                    "step_id": "sequence",
                    "type": 1.0,
                    "value": "20,30",
                    "coord_sequence_en": True,
                    "coord_sequence_points": "30,40; 50,60",
                },
                {
                    "step_id": "stepping",
                    "type": 1.0,
                    "value": "70,80",
                    "coord_step_en": True,
                    "coord_step_direction": "向下",
                    "coord_step_distance": "10",
                    "coord_step_max_steps": "2",
                },
                {"step_id": "hover", "type": 8.0, "value": "90,100"},
                {
                    "step_id": "drag",
                    "type": 10.0,
                    "value": "110,120 -> 130,140",
                },
            ]
        )

        editable = [
            (source.task.step_id, source.kind, source.point_index)
            for source in plan.point_sources
            if source.editable
        ]
        self.assertEqual(
            editable,
            [
                ("direct", "value", -1),
                ("sequence", "sequence", 0),
                ("sequence", "sequence", 1),
            ],
        )

    def test_line_scope_uses_deepest_common_group_and_implicit_ungrouped_group(self):
        tasks = [
            {"step_id": "u1", "type": 1.0, "value": "0,0"},
            {"step_id": "u2", "type": 1.0, "value": "10,0"},
            {
                "step_id": "a_root",
                "type": 1.0,
                "value": "20,0",
                "editor_groups": self._group("A"),
            },
            {
                "step_id": "a_child_1",
                "type": 1.0,
                "value": "30,0",
                "editor_groups": self._group("A", "A_CHILD"),
            },
            {
                "step_id": "a_child_2",
                "type": 1.0,
                "value": "40,0",
                "editor_groups": self._group("A", "A_CHILD"),
            },
            {
                "step_id": "a_tail",
                "type": 1.0,
                "value": "50,0",
                "editor_groups": self._group("A"),
            },
            {
                "step_id": "b1",
                "type": 1.0,
                "value": "60,0",
                "editor_groups": self._group("B"),
            },
            {
                "step_id": "b2",
                "type": 1.0,
                "value": "70,0",
                "editor_groups": self._group("B"),
            },
            {"step_id": "u3", "type": 1.0, "value": "80,0"},
            {"step_id": "u4", "type": 1.0, "value": "90,0"},
        ]
        plan = build_coordinate_preview(tasks)
        segments = {
            (segment["from"], segment["to"]): segment
            for segment in plan.line_segments
        }

        self.assertEqual(
            {ref.step_id for ref in segments[(0, 1)]["move_task_refs"]},
            {"u1", "u2", "u3", "u4"},
        )
        self.assertEqual(segments[(1, 2)]["blocked_reason"], "cross_group")
        self.assertEqual(
            {ref.step_id for ref in segments[(2, 3)]["move_task_refs"]},
            {"a_root", "a_child_1", "a_child_2", "a_tail"},
        )
        self.assertEqual(
            {ref.step_id for ref in segments[(3, 4)]["move_task_refs"]},
            {"a_child_1", "a_child_2"},
        )
        self.assertEqual(
            {ref.step_id for ref in segments[(4, 5)]["move_task_refs"]},
            {"a_root", "a_child_1", "a_child_2", "a_tail"},
        )
        self.assertEqual(segments[(5, 6)]["blocked_reason"], "cross_group")
        self.assertEqual(segments[(7, 8)]["blocked_reason"], "cross_group")
        self.assertEqual(
            {ref.step_id for ref in segments[(8, 9)]["move_task_refs"]},
            {"u1", "u2", "u3", "u4"},
        )

    def test_point_edit_resolves_stable_step_id_after_reorder(self):
        original = [
            {"step_id": "first", "type": 1.0, "value": "10,10"},
            {
                "step_id": "second",
                "type": 1.0,
                "value": "20,20",
                "coord_sequence_en": True,
                "coord_sequence_points": "30,30; 40,40",
            },
        ]
        plan = build_coordinate_preview(original)
        source = next(
            source
            for source in plan.point_sources
            if source.task.step_id == "second" and source.point_index == 1
        )

        updated = apply_preview_point_position(
            list(reversed(original)), source, (75, 85)
        )

        self.assertEqual(updated[0]["step_id"], "second")
        self.assertEqual(updated[0]["coord_sequence_points"], "30,30; 75,85")
        self.assertEqual(updated[1]["value"], "10,10")

    def test_group_translation_moves_every_static_coordinate_form(self):
        tasks = [
            {"step_id": "direct", "type": 1.0, "value": "10,20"},
            {
                "step_id": "sequence",
                "type": 1.0,
                "value": "20,30",
                "coord_sequence_en": True,
                "coord_sequence_points": "30,40; 50,60",
            },
            {
                "step_id": "step",
                "type": 1.0,
                "value": "70,80",
                "coord_step_en": True,
                "coord_step_direction": "移动到新点位",
                "coord_step_point": "90,100",
                "coord_step_manual_points": '{"1": [80, 90]}',
            },
            {"step_id": "hover", "type": 8.0, "value": "110,120"},
            {
                "step_id": "drag",
                "type": 10.0,
                "value": "130,140 -> 150,160",
            },
        ]
        plan = build_coordinate_preview(tasks)
        refs = tuple(source.task for source in plan.point_sources)

        updated = translate_preview_task_positions(tasks, refs, 5, -2)

        self.assertEqual(updated[0]["value"], "15,18")
        self.assertEqual(updated[1]["coord_sequence_points"], "35,38; 55,58")
        self.assertEqual(updated[1]["value"], "20,30")
        self.assertEqual(updated[2]["value"], "75,78")
        self.assertEqual(updated[2]["coord_step_point"], "95,98")
        self.assertEqual(updated[2]["coord_step_manual_points"], '{"1": [85, 88]}')
        self.assertEqual(updated[3]["value"], "115,118")
        self.assertEqual(updated[4]["value"], "135,138 -> 155,158")

    def test_truncated_preview_disables_all_line_translation(self):
        plan = build_coordinate_preview(
            [
                {"step_id": "one", "type": 1.0, "value": "0,0"},
                {"step_id": "two", "type": 1.0, "value": "10,10"},
            ],
            max_points=2,
        )
        self.assertTrue(plan.truncated)
        self.assertTrue(plan.line_segments)
        self.assertTrue(
            all(
                segment.get("blocked_reason") == "truncated"
                and not segment.get("move_indices")
                for segment in plan.line_segments
            )
        )


class MappingBackendTests(unittest.TestCase):
    def test_uia_activation_precedes_post_message_for_single_left_click(self):
        uia = mock.Mock()
        uia.activate.return_value = {
            "success": True,
            "method": "UI Automation Invoke",
        }
        backend = WindowMappingBackend(uia_backend=uia)
        with mock.patch.object(
            backend, "resolve_binding", return_value=(123, 10, 20)
        ), mock.patch.object(
            backend, "screen_point_for_target", return_value=(210, 320)
        ), mock.patch.object(
            user32, "GetAncestor", return_value=100
        ), mock.patch.object(user32, "PostMessageW") as post:
            self.assertTrue(backend.background_click({"root_hwnd": 100}, "left", 1))

        uia.activate.assert_called_once_with(
            100,
            210,
            320,
            preferred_hwnd=123,
        )
        post.assert_not_called()
        self.assertEqual(backend.last_background_method, "UI Automation Invoke")

    def test_uia_failure_falls_back_to_post_message_without_foreground_click(self):
        uia = mock.Mock()
        uia.activate.return_value = {"success": False, "error": "not supported"}
        backend = WindowMappingBackend(uia_backend=uia)
        with mock.patch.object(
            backend, "resolve_binding", return_value=(123, 10, 20)
        ), mock.patch.object(
            backend, "screen_point_for_target", return_value=(210, 320)
        ), mock.patch.object(
            user32, "GetAncestor", return_value=100
        ), mock.patch.object(
            user32, "PostMessageW", return_value=True
        ) as post, mock.patch("fukua_rpa.mapping_backend.time.sleep"):
            self.assertTrue(backend.background_click({"root_hwnd": 100}, "left", 1))

        self.assertEqual(post.call_count, 3)
        self.assertEqual(backend.last_background_method, "PostMessage")

    def test_uia_timeout_never_sends_a_second_background_action(self):
        uia = mock.Mock()
        uia.activate.return_value = {
            "success": False,
            "timed_out": True,
            "outcome_unknown": True,
        }
        backend = WindowMappingBackend(uia_backend=uia)
        with mock.patch.object(
            backend, "resolve_binding", return_value=(123, 10, 20)
        ), mock.patch.object(
            backend, "screen_point_for_target", return_value=(210, 320)
        ), mock.patch.object(
            user32, "GetAncestor", return_value=100
        ), mock.patch.object(user32, "PostMessageW") as post:
            self.assertFalse(backend.background_click({"root_hwnd": 100}, "left", 1))

        post.assert_not_called()
        self.assertIn("未重复点击", backend.last_background_method)

    def test_manual_window_selection_is_separate_from_click_coordinate(self):
        backend = WindowMappingBackend()
        binding = {"root_hwnd": 700, "root_title": "Target"}
        with mock.patch.object(
            backend, "root_window_at_point", return_value=700
        ) as select_root, mock.patch.object(
            backend, "create_binding_for_root", return_value=binding
        ) as create:
            result = backend.create_binding_for_window_at_point(900, 500, 120, 80)

        self.assertEqual(result, binding)
        select_root.assert_called_once_with(900, 500, exclude_current_process=True)
        create.assert_called_once_with(700, 120, 80)

    def test_bound_coordinate_offsets_follow_step_and_custom_points(self):
        binding = {
            "root_hwnd": 700,
            "root_client_x": 120,
            "root_client_y": 80,
            "source_x": 320,
            "source_y": 240,
            "target_hwnd": 701,
            "target_client_x": 20,
            "target_client_y": 10,
        }

        adjusted = WindowMappingBackend.binding_for_coordinate(binding, 350, 260)

        self.assertEqual(adjusted["root_client_x"], 150)
        self.assertEqual(adjusted["root_client_y"], 100)
        self.assertEqual(adjusted["source_x"], 350)
        self.assertEqual(adjusted["source_y"], 260)
        self.assertNotIn("target_hwnd", adjusted)
        self.assertEqual(binding["root_client_x"], 120)

    def test_background_click_uses_bound_client_coordinates(self):
        backend = WindowMappingBackend()
        with mock.patch.object(backend, "resolve_binding", return_value=(123, 10, 20)), mock.patch.object(
            user32, "PostMessageW", return_value=True
        ) as post, mock.patch("fukua_rpa.mapping_backend.time.sleep"):
            self.assertTrue(backend.background_click({"root_hwnd": 123}, "left", 2))
        self.assertEqual(post.call_count, 5)
        self.assertTrue(all(call.args[0] == 123 for call in post.call_args_list))

    def test_background_click_returns_false_when_every_post_message_fails(self):
        backend = WindowMappingBackend()
        with mock.patch.object(
            backend, "resolve_binding", return_value=(123, 10, 20)
        ), mock.patch.object(
            user32, "PostMessageW", return_value=False
        ) as post, mock.patch("fukua_rpa.mapping_backend.time.sleep"):
            self.assertFalse(
                backend.background_click({"root_hwnd": 123}, "left", 2)
            )
        # PostMessage was attempted but no message was accepted; the method must
        # report failure and must not claim a successful PostMessage delivery.
        self.assertGreater(post.call_count, 0)
        self.assertEqual(backend.last_background_method, "")

    def test_process_path_binding_takes_priority_over_window_title(self):
        backend = WindowMappingBackend()
        expected = os.path.normcase(os.path.abspath("C:/Program Files/App/app.exe"))
        with mock.patch.object(user32, "IsWindow", return_value=True), mock.patch.object(
            backend, "class_name", return_value="Widget"
        ), mock.patch.object(backend, "process_info", return_value=(42, expected)), mock.patch.object(
            backend, "title", return_value="Changed title"
        ):
            self.assertTrue(
                backend.matches_binding(
                    123,
                    {
                        "root_class": "Widget",
                        "process_path": expected,
                        "root_title": "Old title",
                        "pid": 9,
                    },
                )
            )


class ChildEnumerationBoundTests(unittest.TestCase):
    """FR-20261006-017：子窗口枚举必须有数量上限并提前终止。

    病态目标（窗口化浏览器、多面板 IDE、虚拟化列表宿主）可达数十万子窗口，
    每个都要做 GetWindowRect/IsWindowVisible。该工作发生在运行循环内会累积延迟，
    因此与同领域有界约定对齐：``window_diagnostics`` 128、``uia_backend`` 384。
    """

    def _backend_with_fake_enum(self, total_children, area_for):
        backend = WindowMappingBackend(uia_backend=mock.Mock())
        seen = {"count": 0}

        def fake_enum(_root, callback, _lparam):
            for index in range(total_children):
                seen["count"] += 1
                if not callback(index + 1000, 0):
                    # Returning False must stop enumeration immediately.
                    break
            return True

        # ``expected_root`` 路径先经``user32.IsWindow`` 校验根窗口；测试环境没有
        # 真实 HWND，必须一并放行，否则会在枚举之前就返回 None。
        with mock.patch.object(
            user32, "EnumChildWindows", fake_enum
        ), mock.patch.object(user32, "IsWindow", return_value=True):
            result = backend.background_click_target(
                100,
                200,
                expected_root=1,
                area_callback=area_for,
            )
        return result, seen["count"]

    def test_enumeration_stops_at_the_configured_limit(self):
        """20 万子窗口时评估次数必须被上限约束，而不是全部遍历。"""
        areas = {"n": 0}

        def area_for(hwnd, _x, _y):
            areas["n"] += 1
            # 只让前 3 个候选可见面积最小，其余更大。
            return (100, 100, 50, 50) if areas["n"] <= 3 else (400, 400, 10, 10)

        _result, visited = self._backend_with_fake_enum(200000, area_for)
        self.assertLessEqual(
            visited,
            _CHILD_ENUM_MAX_CANDIDATES + 2,
            f"枚举未受限：访问了 {visited} 个子窗口",
        )

    def test_small_target_is_not_truncated(self):
        """正常控件树必须完整遍历，语义不变。"""

        def area_for(hwnd, _x, _y):
            return (100, 100, 10, 10)

        result, visited = self._backend_with_fake_enum(50, area_for)
        self.assertEqual(visited, 50, "正常规模必须完整枚举")
        self.assertIsNotNone(result)

    def test_smallest_area_still_wins_within_the_limit(self):
        """上限不得改变既有「最小可见面积优先」语义。"""

        def area_for(hwnd, _x, _y):
            # 第 3 个枚举到的子窗口面积最小，应当胜出。
            return (10, 10, 5, 5) if hwnd == 1002 else (900, 900, 400, 400)

        result, _visited = self._backend_with_fake_enum(20, area_for)
        self.assertEqual(result, 1002)


if __name__ == "__main__":
    unittest.main()
