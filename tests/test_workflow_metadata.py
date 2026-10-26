import os
import tempfile
import unittest
from unittest import mock

import pyperclip

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QInputDialog

from fukua_rpa.workflow_document import merge_workflow_tasks
from fukua_rpa.workflow_metadata import (
    MAX_GROUP_DEPTH,
    build_group_presentations,
    isolate_editor_group_identities_for_insert,
    normalize_group_path,
    rebuild_editor_group_identities,
    remap_editor_group_identities,
    repair_discontiguous_group_paths,
)
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window


class WorkflowMetadataModelTests(unittest.TestCase):
    def test_group_path_is_bounded_to_three_levels(self):
        path = normalize_group_path(
            [
                {"id": f"group_{index}", "name": f"G{index}"}
                for index in range(5)
            ]
        )
        self.assertEqual(len(path), MAX_GROUP_DEPTH)
        self.assertEqual([item["name"] for item in path], ["G0", "G1", "G2"])

    def test_normalize_preserves_repeated_id_layers_with_unique_output(self):
        normalized = normalize_group_path(
            [
                {"id": "same_path", "name": "Top", "collapsed": False},
                {"id": "same_path", "name": "Inner", "collapsed": True},
                {"id": "leaf", "name": "Leaf", "collapsed": False},
            ]
        )

        self.assertEqual([group["name"] for group in normalized], ["Top", "Inner", "Leaf"])
        self.assertEqual(len({group["id"] for group in normalized}), 3)
        self.assertEqual(normalized[0]["id"], "same_path")
        self.assertNotEqual(normalized[1]["id"], "same_path")
        self.assertEqual(normalized[2]["id"], "leaf")
        self.assertTrue(normalized[1]["collapsed"])
        self.assertEqual(normalize_group_path(normalized), normalized)

    def test_repair_preserves_consecutive_malformed_paths_and_is_idempotent(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=["repaired_inner", "repaired_leaf"],
        ):
            repaired, split_count = repair_discontiguous_group_paths(
                [
                    {"editor_groups": malformed},
                    {"editor_groups": malformed},
                ]
            )

        expected_ids = ["same_path", "repaired_inner", "repaired_leaf"]
        self.assertEqual(split_count, 1)
        self.assertEqual(
            [group["name"] for group in repaired[0]["editor_groups"]],
            ["Top", "Inner", "Leaf"],
        )
        self.assertEqual(
            [group["id"] for group in repaired[0]["editor_groups"]],
            expected_ids,
        )
        self.assertEqual(
            repaired[0]["editor_groups"], repaired[1]["editor_groups"]
        )
        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=AssertionError("idempotent repair allocated another ID"),
        ):
            repeated, repeated_split_count = repair_discontiguous_group_paths(
                repaired
            )
        self.assertEqual(repeated_split_count, 0)
        self.assertEqual(repeated, repaired)

    def test_remap_all_preserves_malformed_three_level_batch(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        remapped = remap_editor_group_identities(
            [
                {"editor_groups": malformed},
                {"editor_groups": malformed},
            ]
        )

        first_path = remapped[0]["editor_groups"]
        self.assertEqual([group["name"] for group in first_path], ["Top", "Inner", "Leaf"])
        self.assertEqual(len({group["id"] for group in first_path}), 3)
        self.assertEqual(first_path, remapped[1]["editor_groups"])
        self.assertTrue(
            {group["id"] for group in first_path}.isdisjoint(
                {"same_path", "leaf"}
            )
        )

    def test_remap_is_idempotent_without_allocating_again(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        first = remap_editor_group_identities(
            [
                {"editor_groups": malformed},
                {"editor_groups": malformed},
            ]
        )

        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=AssertionError("idempotent remap allocated another ID"),
        ):
            second = remap_editor_group_identities(first)

        self.assertEqual(second, first)

    def test_rebuild_remap_all_is_idempotent_for_explicit_namespace(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        first, first_split_count = rebuild_editor_group_identities(
            [
                {"editor_groups": malformed},
                {"editor_groups": malformed},
            ],
            remap_all=True,
            namespace="stable-import",
        )
        self.assertEqual(first_split_count, 1)

        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=AssertionError("idempotent rebuild allocated another ID"),
        ):
            second, second_split_count = rebuild_editor_group_identities(
                first,
                remap_all=True,
                namespace="stable-import",
            )

        self.assertEqual(second_split_count, 0)
        self.assertEqual(second, first)

    def test_insert_isolation_namespace_is_explicit_and_repeatable(self):
        tasks = [
            {
                "editor_groups": [
                    {"id": "source_group", "name": "Source", "collapsed": False}
                ]
            }
        ]
        first = isolate_editor_group_identities_for_insert(
            tasks, namespace="batch-a"
        )
        repeated = isolate_editor_group_identities_for_insert(
            tasks, namespace="batch-a"
        )
        other = isolate_editor_group_identities_for_insert(
            tasks, namespace="batch-b"
        )

        self.assertEqual(repeated, first)
        self.assertNotEqual(
            first[0]["editor_groups"][0]["id"],
            other[0]["editor_groups"][0]["id"],
        )

    def test_reserved_iterable_types_consistently_avoid_attempt_zero(self):
        tasks = [
            {
                "editor_groups": [
                    {"id": "source_group", "name": "Source", "collapsed": False}
                ]
            }
        ]
        operations = {
            "remap": lambda source, reserved: remap_editor_group_identities(
                source,
                reserved_ids=reserved,
                namespace="reserved-iterables",
            ),
            "rebuild": lambda source, reserved: rebuild_editor_group_identities(
                source,
                remap_all=True,
                reserved_ids=reserved,
                namespace="reserved-iterables",
            )[0],
            "isolate": lambda source, reserved: isolate_editor_group_identities_for_insert(
                source,
                reserved_ids=reserved,
                namespace="reserved-iterables",
            ),
        }
        iterable_factories = {
            "list": list,
            "set": set,
            "generator": lambda values: (value for value in values),
        }

        for operation_name, operation in operations.items():
            attempt_zero_result = operation(tasks, ())
            attempt_zero_id = attempt_zero_result[0]["editor_groups"][0]["id"]
            reserved_values = (attempt_zero_id, "destination_group")
            results = []
            for iterable_name, iterable_factory in iterable_factories.items():
                with self.subTest(
                    operation=operation_name,
                    iterable=iterable_name,
                ):
                    result = operation(tasks, iterable_factory(reserved_values))
                    result_ids = {
                        group["id"]
                        for task in result
                        for group in task["editor_groups"]
                    }
                    self.assertTrue(result_ids.isdisjoint(reserved_values))
                    self.assertEqual(
                        operation(
                            result,
                            (value for value in reserved_values),
                        ),
                        result,
                    )
                    results.append(result)
            self.assertEqual(results[1:], [results[0], results[0]])

    def test_reserved_ids_are_stringified_once_and_shared_by_both_stages(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        tasks = [
            {"editor_groups": malformed},
            {"editor_groups": malformed},
        ]
        namespace = "single-materialization"
        attempt_zero = rebuild_editor_group_identities(
            tasks,
            remap_all=True,
            namespace=namespace,
        )[0][0]["editor_groups"][0]["id"]

        class StringOnce:
            def __init__(self, value):
                self.value = value
                self.calls = 0

            def __str__(self):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("reserved ID was stringified more than once")
                return self.value

        temporary_reserved = StringOnce("reserved_temporary_id")
        deterministic_reserved = StringOnce(attempt_zero)
        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=[
                "reserved_temporary_id",
                "repaired_inner",
                "repaired_leaf",
            ],
        ) as new_group_id:
            repaired, split_count = rebuild_editor_group_identities(
                tasks,
                remap_all=True,
                reserved_ids=(
                    value
                    for value in (temporary_reserved, deterministic_reserved)
                ),
                namespace=namespace,
            )

        repaired_ids = {
            group["id"]
            for task in repaired
            for group in task["editor_groups"]
        }
        self.assertEqual(split_count, 1)
        self.assertEqual(new_group_id.call_count, 3)
        self.assertEqual(temporary_reserved.calls, 1)
        self.assertEqual(deterministic_reserved.calls, 1)
        self.assertTrue(
            repaired_ids.isdisjoint({"reserved_temporary_id", attempt_zero})
        )

    def test_legal_three_level_continuous_group_is_not_split(self):
        path = [
            {"id": "outer", "name": "Outer", "collapsed": False},
            {"id": "inner", "name": "Inner", "collapsed": False},
            {"id": "leaf", "name": "Leaf", "collapsed": True},
        ]
        repaired, split_count = repair_discontiguous_group_paths(
            [{"editor_groups": path}, {"editor_groups": path}]
        )

        self.assertEqual(split_count, 0)
        self.assertEqual(repaired[0]["editor_groups"], path)
        self.assertEqual(repaired[1]["editor_groups"], path)

    def test_nested_collapsed_group_only_hides_its_descendants(self):
        outer = {"id": "outer_group", "name": "外层", "collapsed": False}
        inner = {"id": "inner_group", "name": "内层", "collapsed": True}
        tasks = [
            {"editor_groups": [outer]},
            {"editor_groups": [outer, inner]},
            {"editor_groups": [outer, inner]},
            {"editor_groups": [outer]},
        ]

        presentations = build_group_presentations(tasks)

        self.assertEqual(presentations[0]["headers"][0]["count"], 4)
        self.assertFalse(presentations[0]["controls_hidden"])
        self.assertEqual(presentations[1]["headers"][0]["name"], "内层")
        self.assertTrue(presentations[1]["controls_hidden"])
        self.assertTrue(presentations[2]["hide_for_collapse"])
        self.assertFalse(presentations[3]["hide_for_collapse"])

    def test_collapsed_outer_group_hides_nested_header_at_same_start(self):
        outer = {"id": "outer", "name": "外层", "collapsed": True}
        inner = {"id": "inner", "name": "内层", "collapsed": False}
        presentations = build_group_presentations(
            [
                {"editor_groups": [outer, inner]},
                {"editor_groups": [outer, inner]},
            ]
        )

        self.assertEqual(
            [header["id"] for header in presentations[0]["headers"]],
            ["outer"],
        )
        self.assertTrue(presentations[0]["controls_hidden"])
        self.assertTrue(presentations[1]["hide_for_collapse"])

    def test_discontiguous_group_segments_receive_independent_ids(self):
        shared = {"id": "shared", "name": "同一组", "collapsed": False}
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [shared]},
                {"editor_groups": []},
                {"editor_groups": [shared]},
                {"editor_groups": [shared]},
            ]
        )

        self.assertEqual(split_count, 1)
        first_id = repaired[0]["editor_groups"][0]["id"]
        later_id = repaired[2]["editor_groups"][0]["id"]
        self.assertNotEqual(first_id, later_id)
        self.assertEqual(repaired[3]["editor_groups"][0]["id"], later_id)
        self.assertEqual(repaired[2]["editor_groups"][0]["name"], "同一组")

    def test_adjacent_equal_metadata_with_same_id_remains_one_group(self):
        shared = {"id": "shared", "name": "同一组", "collapsed": True}
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [shared]},
                {"editor_groups": [shared]},
            ]
        )

        self.assertEqual(split_count, 0)
        self.assertEqual(
            repaired[0]["editor_groups"][0]["id"],
            repaired[1]["editor_groups"][0]["id"],
        )

    def test_adjacent_group_metadata_conflicts_split_name_or_collapsed(self):
        conflicts = (
            (
                {"id": "shared", "name": "甲", "collapsed": False},
                {"id": "shared", "name": "乙", "collapsed": False},
            ),
            (
                {"id": "shared", "name": "甲", "collapsed": False},
                {"id": "shared", "name": "甲", "collapsed": True},
            ),
        )
        for first, second in conflicts:
            with self.subTest(second=second):
                repaired, split_count = repair_discontiguous_group_paths(
                    [
                        {"editor_groups": [first]},
                        {"editor_groups": [second]},
                    ]
                )
                self.assertEqual(split_count, 1)
                self.assertNotEqual(
                    repaired[0]["editor_groups"][0]["id"],
                    repaired[1]["editor_groups"][0]["id"],
                )
                self.assertEqual(
                    repaired[1]["editor_groups"][0]["name"], second["name"]
                )
                self.assertEqual(
                    repaired[1]["editor_groups"][0]["collapsed"],
                    second["collapsed"],
                )

    def test_group_id_reused_at_deeper_level_is_split_with_full_suffix(self):
        top = {"id": "shared_depth", "name": "Top", "collapsed": False}
        outer = {"id": "outer_b", "name": "Outer B", "collapsed": False}
        inner = {"id": "shared_depth", "name": "Inner", "collapsed": True}
        leaf = {"id": "leaf", "name": "Leaf", "collapsed": False}

        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=["repaired_inner", "repaired_leaf"],
        ):
            repaired, split_count = repair_discontiguous_group_paths(
                [
                    {"editor_groups": [top]},
                    {"editor_groups": [outer, inner, leaf]},
                    {"editor_groups": [outer, inner, leaf]},
                ]
            )

        self.assertEqual(split_count, 1)
        self.assertEqual(repaired[0]["editor_groups"][0]["id"], "shared_depth")
        self.assertEqual(
            [group["id"] for group in repaired[1]["editor_groups"]],
            ["outer_b", "repaired_inner", "repaired_leaf"],
        )
        self.assertEqual(
            repaired[1]["editor_groups"], repaired[2]["editor_groups"]
        )

    def test_same_child_id_under_different_parents_is_split(self):
        outer_a = {"id": "outer_a", "name": "A", "collapsed": False}
        outer_b = {"id": "outer_b", "name": "B", "collapsed": False}
        child = {"id": "shared_child", "name": "Child", "collapsed": False}
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [outer_a, child]},
                {"editor_groups": [outer_a, child]},
                {"editor_groups": [outer_b, child]},
                {"editor_groups": [outer_b, child]},
            ]
        )

        first_child_id = repaired[0]["editor_groups"][1]["id"]
        second_child_id = repaired[2]["editor_groups"][1]["id"]
        self.assertEqual(split_count, 1)
        self.assertEqual(repaired[1]["editor_groups"][1]["id"], first_child_id)
        self.assertEqual(repaired[3]["editor_groups"][1]["id"], second_child_id)
        self.assertNotEqual(first_child_id, second_child_id)

    def test_three_level_parent_conflict_remaps_descendant_suffix(self):
        outer_a = {"id": "outer_a", "name": "A", "collapsed": False}
        outer_b = {"id": "outer_b", "name": "B", "collapsed": False}
        middle = {"id": "shared_middle", "name": "Middle", "collapsed": False}
        leaf = {"id": "shared_leaf", "name": "Leaf", "collapsed": True}
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [outer_a, middle, leaf]},
                {"editor_groups": [outer_a, middle, leaf]},
                {"editor_groups": [outer_b, middle, leaf]},
                {"editor_groups": [outer_b, middle, leaf]},
            ]
        )

        first_suffix = [
            group["id"] for group in repaired[0]["editor_groups"][1:]
        ]
        second_suffix = [
            group["id"] for group in repaired[2]["editor_groups"][1:]
        ]
        self.assertGreaterEqual(split_count, 1)
        self.assertTrue(set(first_suffix).isdisjoint(second_suffix))
        self.assertEqual(
            repaired[2]["editor_groups"], repaired[3]["editor_groups"]
        )

    def test_remap_all_keeps_cross_depth_semantic_groups_distinct(self):
        top = {"id": "shared_depth", "name": "Top", "collapsed": False}
        outer = {"id": "outer_b", "name": "Outer B", "collapsed": False}
        inner = {"id": "shared_depth", "name": "Inner", "collapsed": False}
        remapped = remap_editor_group_identities(
            [
                {"editor_groups": [top]},
                {"editor_groups": [outer, inner]},
                {"editor_groups": [outer, inner]},
            ]
        )

        top_id = remapped[0]["editor_groups"][0]["id"]
        inner_id = remapped[1]["editor_groups"][1]["id"]
        self.assertNotEqual(top_id, inner_id)
        self.assertEqual(remapped[2]["editor_groups"][1]["id"], inner_id)
        self.assertTrue(
            {top_id, inner_id}.isdisjoint({"shared_depth", "outer_b"})
        )

    def test_long_group_ids_with_the_same_prefix_do_not_collapse(self):
        shared_prefix = "x" * 80
        first_id = shared_prefix + "-first"
        second_id = shared_prefix + "-second"
        first_path = normalize_group_path(
            [{"id": first_id, "name": "同名", "collapsed": False}]
        )
        second_path = normalize_group_path(
            [{"id": second_id, "name": "同名", "collapsed": False}]
        )

        self.assertLessEqual(len(first_path[0]["id"]), 80)
        self.assertLessEqual(len(second_path[0]["id"]), 80)
        self.assertNotEqual(first_path[0]["id"], second_path[0]["id"])
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": first_path},
                {"editor_groups": second_path},
            ]
        )
        repeated, repeated_split_count = repair_discontiguous_group_paths(repaired)
        self.assertEqual(split_count, 0)
        self.assertEqual(repeated_split_count, 0)
        self.assertEqual(repeated, repaired)

    def test_ancestor_metadata_conflict_isolates_all_descendant_ids(self):
        first_path = [
            {"id": "outer", "name": "甲", "collapsed": False},
            {"id": "middle", "name": "中层", "collapsed": False},
            {"id": "inner", "name": "内层", "collapsed": False},
        ]
        conflicting_path = [
            {"id": "outer", "name": "乙", "collapsed": False},
            {"id": "middle", "name": "中层", "collapsed": False},
            {"id": "inner", "name": "内层", "collapsed": False},
        ]
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": first_path},
                {"editor_groups": conflicting_path},
                {"editor_groups": conflicting_path},
            ]
        )

        first_ids = [group["id"] for group in repaired[0]["editor_groups"]]
        conflicting_ids = [
            group["id"] for group in repaired[1]["editor_groups"]
        ]
        self.assertGreaterEqual(split_count, 1)
        self.assertTrue(
            all(
                first != second
                for first, second in zip(first_ids, conflicting_ids, strict=True)
            )
        )
        self.assertEqual(
            conflicting_ids,
            [group["id"] for group in repaired[2]["editor_groups"]],
        )

    def test_group_identity_repair_is_idempotent(self):
        shared = {"id": "shared", "name": "甲", "collapsed": False}
        first, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [shared]},
                {
                    "editor_groups": [
                        {"id": "shared", "name": "乙", "collapsed": False}
                    ]
                },
            ]
        )
        self.assertEqual(split_count, 1)

        with mock.patch(
            "fukua_rpa.workflow_metadata.new_editor_group_id",
            side_effect=AssertionError("idempotent repair allocated another ID"),
        ):
            second, second_split_count = repair_discontiguous_group_paths(first)

        self.assertEqual(second_split_count, 0)
        self.assertEqual(second, first)

    def test_three_level_split_remaps_the_complete_nested_suffix(self):
        outer = {"id": "outer", "name": "外层"}
        middle = {"id": "middle", "name": "中层"}
        inner = {"id": "inner", "name": "内层"}
        repaired, split_count = repair_discontiguous_group_paths(
            [
                {"editor_groups": [outer, middle, inner]},
                {"editor_groups": []},
                {"editor_groups": [outer, middle, inner]},
                {"editor_groups": [outer, middle, inner]},
            ]
        )

        first_ids = [group["id"] for group in repaired[0]["editor_groups"]]
        later_ids = [group["id"] for group in repaired[2]["editor_groups"]]
        self.assertGreaterEqual(split_count, 1)
        self.assertTrue(
            all(old != new for old, new in zip(first_ids, later_ids, strict=True))
        )
        self.assertEqual(
            later_ids,
            [group["id"] for group in repaired[3]["editor_groups"]],
        )

    def test_collapsed_hidden_anchor_is_part_of_group_presentation(self):
        collapsed = {"id": "collapsed", "name": "折叠组", "collapsed": True}
        presentations = build_group_presentations(
            [
                {"editor_hidden": True, "editor_groups": [collapsed]},
                {"editor_hidden": True, "editor_groups": [collapsed]},
            ]
        )

        self.assertTrue(presentations[0]["collapsed_header_anchor"])
        self.assertFalse(presentations[1]["collapsed_header_anchor"])


class WorkflowMetadataUiTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        # Isolate the system clipboard so copy/cut tests never leak step JSON
        # (e.g. the "原组" fixture) onto the real clipboard where a later app
        # session would offer it as a pasteable group.
        buffer = []
        real_copy = pyperclip.copy
        real_paste = pyperclip.paste

        def fake_copy(text):
            buffer.append(text)

        def fake_paste():
            return buffer[-1] if buffer else ""

        pyperclip.copy = fake_copy
        pyperclip.paste = fake_paste

        def restore_clipboard():
            pyperclip.copy = real_copy
            pyperclip.paste = real_paste

        self.addCleanup(restore_clipboard)

    def create_window(self, base_dir):
        window = create_test_window(self, base_dir)
        advanced = window.settings_mode_combo.findData("advanced")
        window.settings_mode_combo.setCurrentIndex(advanced)
        window.apply_settings_mode()
        return window

    def test_group_creation_and_collapse_preserve_flat_runtime_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            for index in range(4):
                window.add_row(
                    {"type": 1.0, "value": f"{index},{index}"},
                    record_undo=False,
                    select=False,
                )
            second = window.task_list.item(1)
            third = window.task_list.item(2)
            window.task_list.select_item_with_modifiers(second, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(third, Qt.ControlModifier)

            with mock.patch.object(
                QInputDialog, "getText", return_value=("答题步骤", True)
            ):
                self.assertTrue(window.workflow_editor.create_group_from_selection())

            snapshot = window.snapshot_tasks()
            first_group = snapshot[1]["editor_groups"][0]
            self.assertEqual(first_group["name"], "答题步骤")
            self.assertEqual(
                snapshot[2]["editor_groups"][0]["id"], first_group["id"]
            )
            grouped_row = window.ensure_task_item_widget(window.task_list.item(1))
            self.assertFalse(grouped_row.group_header_frames[0].isHidden())
            self.assertEqual(grouped_row.group_header_counts[0].text(), "2 步")

            self.assertTrue(
                window.workflow_editor.toggle_group_collapsed(first_group["id"])
            )
            self.app.processEvents()
            self.assertFalse(window.task_list.item(1).isHidden())
            self.assertTrue(window.task_list.item(2).isHidden())
            grouped_row = window.ensure_task_item_widget(window.task_list.item(1))
            self.assertTrue(grouped_row.controls_widget.isHidden())
            self.assertEqual(len(window.snapshot_tasks()), 4)

    def test_dragging_part_of_group_across_an_unrelated_step_detaches_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            group = {"id": "group_shared", "name": "前三步", "collapsed": False}
            for index in range(4):
                window.add_row(
                    {
                        "type": 1.0,
                        "value": f"{index},{index}",
                        "editor_groups": [group] if index < 3 else [],
                    },
                    record_undo=False,
                    select=False,
                )
            moved_id = window.snapshot_tasks()[1]["step_id"]
            moved_item = window.task_list.takeItem(1)
            window.task_list.insertItem(3, moved_item)

            self.assertEqual(
                window.workflow_editor.handle_tasks_reordered([moved_id]), 1
            )

            snapshot = window.snapshot_tasks()
            self.assertEqual(snapshot[0]["editor_groups"][0]["id"], "group_shared")
            self.assertEqual(snapshot[1]["editor_groups"][0]["id"], "group_shared")
            self.assertEqual(snapshot[3]["step_id"], moved_id)
            self.assertEqual(snapshot[3]["editor_groups"], [])

    def test_dragging_an_ungrouped_step_inside_a_group_splits_the_group(self):
        # Regression (fixed): dragging an ungrouped step *into* a group should
        # fold it into that existing group (sharing the same id) instead of
        # leaving it outside and letting repair split the group into two
        # same-named segments.
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            group = {"id": "group_shared", "name": "原分组", "collapsed": False}
            for index in range(3):
                window.add_row(
                    {
                        "type": 1.0,
                        "value": f"{index},{index}",
                        "editor_groups": [group] if index < 2 else [],
                    },
                    record_undo=False,
                    select=False,
                )
            moved_id = window.snapshot_tasks()[2]["step_id"]
            moved_item = window.task_list.takeItem(2)
            window.task_list.insertItem(1, moved_item)

            # No segment is left dangling now, so nothing is split or trimmed.
            self.assertEqual(
                window.workflow_editor.handle_tasks_reordered([moved_id]), 0
            )

            snapshot = window.snapshot_tasks()
            self.assertEqual(snapshot[0]["editor_groups"][0]["id"], "group_shared")
            self.assertEqual(snapshot[1]["editor_groups"][0]["id"], "group_shared")
            self.assertEqual(snapshot[2]["editor_groups"][0]["id"], "group_shared")

    def test_restore_snapshot_repairs_discontiguous_group_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            shared = {"id": "shared_group", "name": "导入组"}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [shared],
                    },
                    {"step_id": "stable_02", "type": 1.0, "value": "2,2"},
                    {
                        "step_id": "stable_03",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_groups": [shared],
                    },
                ]
            )

            snapshot = window.snapshot_tasks()
            self.assertEqual(
                [task["step_id"] for task in snapshot],
                ["stable_01", "stable_02", "stable_03"],
            )
            self.assertNotEqual(
                snapshot[0]["editor_groups"][0]["id"],
                snapshot[2]["editor_groups"][0]["id"],
            )

    def test_restore_and_update_indexes_are_idempotent_for_metadata_conflicts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [
                            {"id": "shared", "name": "甲", "collapsed": False}
                        ],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [
                            {"id": "shared", "name": "乙", "collapsed": True}
                        ],
                    },
                ]
            )
            first = window.snapshot_tasks()

            window.update_indexes()
            second = window.snapshot_tasks()
            window.restore_task_snapshot(second)
            third = window.snapshot_tasks()

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertNotEqual(
                third[0]["editor_groups"][0]["id"],
                third[1]["editor_groups"][0]["id"],
            )

    def test_restore_and_update_indexes_are_idempotent_for_cross_depth_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            top = {"id": "shared_depth", "name": "Top", "collapsed": False}
            outer = {"id": "outer_b", "name": "Outer B", "collapsed": False}
            inner = {
                "id": "shared_depth",
                "name": "Inner",
                "collapsed": False,
            }
            leaf = {"id": "leaf", "name": "Leaf", "collapsed": False}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [top],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [outer, inner, leaf],
                    },
                    {
                        "step_id": "stable_03",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_groups": [outer, inner, leaf],
                    },
                ]
            )
            first = window.snapshot_tasks()

            with mock.patch(
                "fukua_rpa.workflow_metadata.new_editor_group_id",
                side_effect=AssertionError("idempotent restore allocated another ID"),
            ):
                window.update_indexes()
                second = window.snapshot_tasks()
                window.restore_task_snapshot(second)
                third = window.snapshot_tasks()

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertNotEqual(
                third[0]["editor_groups"][0]["id"],
                third[1]["editor_groups"][1]["id"],
            )
            self.assertEqual(
                third[1]["editor_groups"], third[2]["editor_groups"]
            )

    def test_restore_preserves_malformed_three_level_paths_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            malformed = [
                {"id": "same_path", "name": "Top", "collapsed": False},
                {"id": "same_path", "name": "Inner", "collapsed": True},
                {"id": "leaf", "name": "Leaf", "collapsed": False},
            ]
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": malformed,
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": malformed,
                    },
                ]
            )
            first = window.snapshot_tasks()

            self.assertEqual(
                [group["name"] for group in first[0]["editor_groups"]],
                ["Top", "Inner", "Leaf"],
            )
            self.assertEqual(len(first[0]["editor_groups"]), 3)
            self.assertEqual(
                len({group["id"] for group in first[0]["editor_groups"]}), 3
            )
            self.assertEqual(
                first[0]["editor_groups"], first[1]["editor_groups"]
            )
            with mock.patch(
                "fukua_rpa.workflow_metadata.new_editor_group_id",
                side_effect=AssertionError("idempotent restore allocated another ID"),
            ):
                window.update_indexes()
                second = window.snapshot_tasks()
                window.restore_task_snapshot(second)
                third = window.snapshot_tasks()
            self.assertEqual(first, second)
            self.assertEqual(second, third)

    def test_malformed_path_top_edits_and_ungroup_do_not_change_inner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            top = {"id": "same_path", "name": "Top", "collapsed": False}
            inner = {"id": "same_path", "name": "Inner", "collapsed": False}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [top],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [top, inner],
                    },
                    {
                        "step_id": "stable_03",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_groups": [top, inner],
                    },
                ]
            )
            normalized = window.snapshot_tasks()
            top_id = normalized[0]["editor_groups"][0]["id"]
            inner_id = normalized[1]["editor_groups"][1]["id"]
            self.assertNotEqual(top_id, inner_id)
            window.task_list.setCurrentRow(0)

            with mock.patch.object(
                QInputDialog, "getText", return_value=("Top renamed", True)
            ):
                self.assertTrue(window.workflow_editor.rename_selected_group())
            self.assertTrue(window.workflow_editor.toggle_group_collapsed(top_id))

            edited = window.snapshot_tasks()
            self.assertTrue(
                all(
                    task["editor_groups"][0]["name"] == "Top renamed"
                    and task["editor_groups"][0]["collapsed"]
                    for task in edited
                )
            )
            self.assertEqual(edited[1]["editor_groups"][1]["id"], inner_id)
            self.assertEqual(edited[1]["editor_groups"][1]["name"], "Inner")
            self.assertFalse(edited[1]["editor_groups"][1]["collapsed"])

            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            self.assertTrue(window.workflow_editor.ungroup_selected_steps())
            ungrouped = window.snapshot_tasks()
            self.assertEqual(ungrouped[0]["editor_groups"], [])
            self.assertEqual(ungrouped[1]["editor_groups"][1]["id"], inner_id)
            self.assertEqual(ungrouped[2]["editor_groups"][1]["id"], inner_id)

    def test_malformed_path_inner_edits_and_ungroup_do_not_change_top(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            top = {"id": "same_path", "name": "Top", "collapsed": False}
            inner = {"id": "same_path", "name": "Inner", "collapsed": False}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [top, inner],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [top, inner],
                    },
                ]
            )
            normalized = window.snapshot_tasks()
            top_id = normalized[0]["editor_groups"][0]["id"]
            inner_id = normalized[0]["editor_groups"][1]["id"]
            self.assertNotEqual(top_id, inner_id)
            window.task_list.setCurrentRow(0)

            with mock.patch.object(
                QInputDialog, "getText", return_value=("Inner renamed", True)
            ):
                self.assertTrue(window.workflow_editor.rename_selected_group())
            self.assertTrue(window.workflow_editor.toggle_group_collapsed(inner_id))

            edited = window.snapshot_tasks()
            self.assertTrue(
                all(
                    task["editor_groups"][0]["id"] == top_id
                    and task["editor_groups"][0]["name"] == "Top"
                    and not task["editor_groups"][0]["collapsed"]
                    for task in edited
                )
            )
            self.assertTrue(
                all(
                    task["editor_groups"][1]["name"] == "Inner renamed"
                    and task["editor_groups"][1]["collapsed"]
                    for task in edited
                )
            )

            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.ControlModifier
            )
            self.assertTrue(window.workflow_editor.ungroup_selected_steps())
            ungrouped = window.snapshot_tasks()
            self.assertTrue(
                all(
                    len(task["editor_groups"]) == 1
                    and task["editor_groups"][0]["id"] == top_id
                    and task["editor_groups"][0]["name"] == "Top"
                    for task in ungrouped
                )
            )

    def test_cross_depth_group_edits_and_ungroup_stay_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            top = {"id": "shared_depth", "name": "Top", "collapsed": False}
            outer = {"id": "outer_b", "name": "Outer B", "collapsed": False}
            inner = {
                "id": "shared_depth",
                "name": "Inner",
                "collapsed": False,
            }
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [top],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [outer, inner],
                    },
                    {
                        "step_id": "stable_03",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_groups": [outer, inner],
                    },
                ]
            )
            normalized = window.snapshot_tasks()
            top_id = normalized[0]["editor_groups"][0]["id"]
            inner_id = normalized[1]["editor_groups"][1]["id"]
            self.assertNotEqual(top_id, inner_id)
            window.task_list.setCurrentRow(1)

            with mock.patch.object(
                QInputDialog, "getText", return_value=("Inner renamed", True)
            ):
                self.assertTrue(window.workflow_editor.rename_selected_group())
            self.assertTrue(window.workflow_editor.toggle_group_collapsed(inner_id))

            edited = window.snapshot_tasks()
            self.assertEqual(edited[0]["editor_groups"][0]["id"], top_id)
            self.assertEqual(edited[0]["editor_groups"][0]["name"], "Top")
            self.assertFalse(edited[0]["editor_groups"][0]["collapsed"])
            self.assertEqual(
                edited[1]["editor_groups"][1]["name"], "Inner renamed"
            )
            self.assertTrue(edited[1]["editor_groups"][1]["collapsed"])

            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(2), Qt.ControlModifier
            )
            self.assertTrue(window.workflow_editor.ungroup_selected_steps())

            ungrouped = window.snapshot_tasks()
            self.assertEqual(ungrouped[0]["editor_groups"], edited[0]["editor_groups"])
            self.assertEqual(len(ungrouped[1]["editor_groups"]), 1)
            self.assertEqual(len(ungrouped[2]["editor_groups"]), 1)
            self.assertEqual(
                ungrouped[1]["editor_groups"][0]["id"],
                ungrouped[2]["editor_groups"][0]["id"],
            )

    def test_merged_source_group_edits_do_not_affect_destination_with_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            destination_group = {
                "id": "shared",
                "name": "目的组",
                "collapsed": False,
            }
            source_group = {
                "id": "shared",
                "name": "来源组",
                "collapsed": False,
            }
            result = merge_workflow_tasks(
                [
                    {
                        "step_id": "dest_0001",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [destination_group],
                    }
                ],
                [
                    {
                        "step_id": "source_01",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [source_group],
                    },
                    {
                        "step_id": "source_02",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_groups": [source_group],
                    },
                ],
            )
            window.restore_task_snapshot(result.tasks)
            window.undo_stack.clear()
            window.redo_stack.clear()
            source_group_id = result.tasks[1]["editor_groups"][0]["id"]
            window.task_list.setCurrentRow(1)

            with mock.patch.object(
                QInputDialog, "getText", return_value=("来源组已改名", True)
            ):
                self.assertTrue(window.workflow_editor.rename_selected_group())
            self.assertTrue(
                window.workflow_editor.toggle_group_collapsed(source_group_id)
            )

            edited = window.snapshot_tasks()
            self.assertEqual(edited[0]["editor_groups"][0]["name"], "目的组")
            self.assertFalse(edited[0]["editor_groups"][0]["collapsed"])
            self.assertEqual(
                edited[1]["editor_groups"][0]["name"], "来源组已改名"
            )
            self.assertTrue(edited[1]["editor_groups"][0]["collapsed"])

            window.undo_task_change()
            self.assertFalse(
                window.snapshot_tasks()[1]["editor_groups"][0]["collapsed"]
            )
            window.undo_task_change()
            self.assertEqual(
                window.snapshot_tasks()[1]["editor_groups"][0]["name"], "来源组"
            )
            window.redo_task_change()
            self.assertEqual(
                window.snapshot_tasks()[1]["editor_groups"][0]["name"],
                "来源组已改名",
            )
            window.redo_task_change()
            replayed = window.snapshot_tasks()
            self.assertTrue(replayed[1]["editor_groups"][0]["collapsed"])
            self.assertEqual(replayed[0]["editor_groups"][0]["name"], "目的组")

    def test_pasting_one_group_member_inside_group_keeps_group_contiguous(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            group = {"id": "original_group", "name": "原组"}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_groups": [group],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_groups": [group],
                    },
                ]
            )
            window.undo_stack.clear()
            window.redo_stack.clear()
            window.task_list.setCurrentRow(0)
            window.copy_selected_row()
            window.paste_row_after_selected()

            pasted = window.snapshot_tasks()
            group_ids = [task["editor_groups"][0]["id"] for task in pasted]
            self.assertEqual(len(set(group_ids)), 1)
            self.assertEqual(pasted[0]["step_id"], "stable_01")
            self.assertEqual(pasted[2]["step_id"], "stable_02")
            self.assertEqual(len(window.undo_stack), 1)

            window.undo_task_change()
            restored = window.snapshot_tasks()
            self.assertEqual(len(restored), 2)
            self.assertEqual(
                restored[0]["editor_groups"][0]["id"],
                restored[1]["editor_groups"][0]["id"],
            )
            window.redo_task_change()
            self.assertEqual(
                len(
                    {
                        task["editor_groups"][0]["id"]
                        for task in window.snapshot_tasks()
                    }
                ),
                1,
            )

    def test_blank_context_paste_appends_instead_of_using_stale_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.restore_task_snapshot(
                [
                    {"type": 1.0, "value": "1,1"},
                    {"type": 1.0, "value": "2,2"},
                ]
            )
            window.task_list.setCurrentRow(0)
            window.copy_selected_row()
            with mock.patch.object(
                window.task_list, "itemAt", return_value=None
            ):
                menu = window.workflow_editor.build_task_context_menu(QPoint(4, 4))

            self.assertEqual(window.task_list.currentRow(), -1)
            paste = next(
                action
                for action in menu.actions()
                if action.text() == "粘贴到步骤末尾"
            )
            paste.trigger()
            self.assertEqual(
                [task["value"] for task in window.snapshot_tasks()],
                ["1,1", "2,2", "1,1"],
            )
            menu.close()
            menu.deleteLater()
            self.app.processEvents()

    def test_collapsed_group_header_context_delete_removes_whole_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            advanced = window.settings_mode_combo.findData("advanced")
            window.settings_mode_combo.setCurrentIndex(advanced)
            group = {
                "id": "delete_group",
                "name": "待删除组",
                "collapsed": True,
            }
            window.restore_task_snapshot(
                [
                    {"type": 1.0, "value": "1,1", "editor_groups": [group]},
                    {"type": 1.0, "value": "2,2", "editor_groups": [group]},
                ]
            )
            window.show()
            self.app.processEvents()
            item = window.task_list.item(0)
            row = window.ensure_task_item_widget(item)
            header_center = row.group_header_frames[0].geometry().center()
            viewport_pos = row.mapTo(window.task_list.viewport(), header_center)
            menu = window.workflow_editor.build_task_context_menu(viewport_pos)

            delete = next(
                action
                for action in menu.actions()
                if action.text() == "删除整个分组"
            )
            delete.trigger()
            self.assertEqual(window.task_list.count(), 0)
            menu.close()
            menu.deleteLater()
            self.app.processEvents()

    def test_virtualized_full_group_copy_rename_and_collapse_do_not_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            group = {"id": "original_group", "name": "原组", "collapsed": False}
            tasks = [
                {
                    "step_id": f"stable_{index:03d}",
                    "type": 1.0,
                    "value": f"{index},{index}",
                    "editor_groups": [group] if index < 2 else [],
                }
                for index in range(87)
            ]
            window.restore_task_snapshot(tasks)
            self.assertTrue(window.task_list.virtualization_active())

            first = window.task_list.item(0)
            second = window.task_list.item(1)
            window.task_list.select_item_with_modifiers(first, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(second, Qt.ControlModifier)
            window.copy_selected_row()
            window.task_list.setCurrentRow(window.task_list.count() - 1)
            window.paste_row_after_selected()

            snapshot = window.snapshot_tasks()
            copied_group_id = snapshot[-1]["editor_groups"][0]["id"]
            self.assertNotEqual(copied_group_id, "original_group")
            self.assertEqual(
                snapshot[-2]["editor_groups"][0]["id"], copied_group_id
            )
            self.assertLess(
                window.task_list.materialized_row_count(), window.task_list.count()
            )

            window.task_list.setCurrentRow(window.task_list.count() - 2)
            self.assertTrue(window.workflow_editor.toggle_group_collapsed(copied_group_id))
            with mock.patch.object(
                QInputDialog, "getText", return_value=("复制组", True)
            ):
                self.assertTrue(window.workflow_editor.rename_selected_group())

            renamed = window.snapshot_tasks()
            self.assertEqual(renamed[0]["editor_groups"][0]["name"], "原组")
            self.assertFalse(renamed[0]["editor_groups"][0]["collapsed"])
            self.assertEqual(renamed[-1]["editor_groups"][0]["name"], "复制组")
            self.assertTrue(renamed[-1]["editor_groups"][0]["collapsed"])

            window.undo_task_change()
            self.assertEqual(
                window.snapshot_tasks()[-1]["editor_groups"][0]["name"], "原组"
            )
            window.redo_task_change()
            self.assertEqual(
                window.snapshot_tasks()[-1]["editor_groups"][0]["name"], "复制组"
            )

    def test_hidden_collapsed_group_keeps_expand_entry_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            outer = {"id": "outer_group", "name": "外层", "collapsed": False}
            inner = {"id": "inner_group", "name": "内层", "collapsed": True}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": "stable_01",
                        "type": 1.0,
                        "value": "1,1",
                        "editor_hidden": True,
                        "editor_groups": [outer],
                    },
                    {
                        "step_id": "stable_02",
                        "type": 1.0,
                        "value": "2,2",
                        "editor_hidden": True,
                        "editor_groups": [outer, inner],
                    },
                    {
                        "step_id": "stable_03",
                        "type": 1.0,
                        "value": "3,3",
                        "editor_hidden": True,
                        "editor_groups": [outer, inner],
                    },
                ]
            )

            inner_anchor = window.task_list.item(1)
            self.assertFalse(inner_anchor.isHidden())
            self.assertTrue(window.task_list.item(2).isHidden())
            row = window.ensure_task_item_widget(inner_anchor)
            self.assertEqual(row.group_header_labels[0].text(), "内层")
            self.assertTrue(row.controls_widget.isHidden())

            simple = window.settings_mode_combo.findData("simple")
            window.settings_mode_combo.setCurrentIndex(simple)
            window.apply_settings_mode()
            self.assertFalse(inner_anchor.isHidden())

            self.assertTrue(window.workflow_editor.toggle_group_collapsed("inner_group"))
            inner_anchor = window.task_list.item(1)
            self.assertTrue(inner_anchor.isHidden())
            self.assertTrue(window.task_list.item(2).isHidden())
            self.assertEqual(len(window.snapshot_tasks()), 3)

    def test_middle_hidden_member_does_not_replace_collapsed_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            group = {"id": "group", "name": "混合隐藏", "collapsed": True}
            window.restore_task_snapshot(
                [
                    {
                        "step_id": f"stable_0{index + 1}",
                        "type": 1.0,
                        "value": f"{index},{index}",
                        "editor_hidden": index == 1,
                        "editor_groups": [group],
                    }
                    for index in range(3)
                ]
            )

            self.assertFalse(window.task_list.item(0).isHidden())
            self.assertTrue(window.task_list.item(1).isHidden())
            self.assertTrue(window.task_list.item(2).isHidden())
            anchor = window.ensure_task_item_widget(window.task_list.item(0))
            self.assertEqual(anchor.group_header_labels[0].text(), "混合隐藏")
            self.assertTrue(anchor.controls_widget.isHidden())

            self.assertTrue(window.workflow_editor.toggle_group_collapsed("group"))
            self.assertFalse(window.task_list.item(0).isHidden())
            self.assertTrue(window.task_list.item(1).isHidden())
            self.assertFalse(window.task_list.item(2).isHidden())

    def test_add_selection_to_group_joins_adjacent_and_rejects_non_adjacent(self):
        # Issue #5: a selected step adjacent to an existing group can be added
        # to that group; a non-adjacent step must be rejected (not silently
        # scattered into the group).
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            for index in range(5):
                window.add_row(
                    {"type": 1.0, "value": f"{index},{index}"},
                    record_undo=False,
                    select=False,
                )
            first = window.task_list.item(0)
            second = window.task_list.item(1)
            window.task_list.select_item_with_modifiers(first, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(second, Qt.ControlModifier)
            with mock.patch.object(
                QInputDialog, "getText", return_value=("组一", True)
            ):
                self.assertTrue(
                    window.workflow_editor.create_group_from_selection()
                )
            group_id = window.snapshot_tasks()[0]["editor_groups"][0]["id"]

            # Adjacent step (row 2) joins the group.
            third = window.task_list.item(2)
            window.task_list.select_item_with_modifiers(third, Qt.NoModifier)
            self.assertTrue(
                window.workflow_editor.add_selection_to_group(group_id)
            )
            joined = window.snapshot_tasks()
            self.assertEqual(joined[0]["editor_groups"][0]["id"], group_id)
            self.assertEqual(joined[1]["editor_groups"][0]["id"], group_id)
            self.assertEqual(joined[2]["editor_groups"][0]["id"], group_id)

            # Non-adjacent step (row 4) must be rejected without mutating state.
            fifth = window.task_list.item(4)
            window.task_list.select_item_with_modifiers(fifth, Qt.NoModifier)
            self.assertFalse(
                window.workflow_editor.add_selection_to_group(group_id)
            )
            unchanged = window.snapshot_tasks()
            self.assertEqual(len(unchanged[4]["editor_groups"]), 0)

    def test_has_paste_content_reflects_clipboard_state(self):
        # Issue #2: the right-click paste action is gated on this; it must track
        # the in-app copy buffer and ignore an empty/invalid system clipboard.
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {"type": 1.0, "value": "1,1"},
                record_undo=False,
                select=False,
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            self.assertFalse(window.workflow_editor.has_paste_content())
            window.copy_selected_row()
            self.assertTrue(window.workflow_editor.has_paste_content())
            # With the in-app buffer cleared and an empty/invalid system
            # clipboard, paste must be disabled again.
            window.workflow_editor.task_clipboard = None
            with mock.patch.object(pyperclip, "paste", return_value=""):
                self.assertFalse(window.workflow_editor.has_paste_content())


if __name__ == "__main__":
    unittest.main()
