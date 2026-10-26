import unittest
from unittest import mock

from fukua_rpa.workflow_document import (
    apply_numeric_reference_edits,
    attach_cloned_tasks_to_insertion_context,
    clone_task_for_insert,
    clone_tasks_for_insert,
    materialize_runtime_references,
    merge_workflow_tasks,
    normalize_workflow_tasks,
    references_to_step,
    remove_task_and_clear_references,
    remove_tasks_and_clear_references,
)


class WorkflowDocumentTests(unittest.TestCase):
    def test_until_defaults_on_normal_steps_never_create_hidden_references(self):
        tasks = normalize_workflow_tasks(
            [
                {"type": 1.0, "value": "1,1", "until_false_jump": "1"},
                {"type": 1.0, "value": "2,2", "until_false_jump": "1"},
            ]
        )
        self.assertEqual(tasks[0]["until_false_target_id"], "")
        self.assertEqual(tasks[1]["until_false_target_id"], "")
        self.assertEqual(references_to_step(tasks, tasks[0]["step_id"]), [])

    def stable_tasks(self):
        return [
            {"step_id": "step_0001", "type": 5.0, "value": "0"},
            {
                "step_id": "step_0002",
                "type": 5.0,
                "value": "0",
                "success_jump": "3",
                "success_target_id": "step_0003",
            },
            {"step_id": "step_0003", "type": 5.0, "value": "0"},
        ]

    def test_legacy_numeric_jumps_migrate_to_stable_ids(self):
        tasks = [
            {"type": 5.0, "value": "0", "success_jump": "2"},
            {"type": 5.0, "value": "0"},
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["generated_0001", "generated_0002"],
        ):
            normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(normalized[0]["success_target_id"], "generated_0002")
        self.assertEqual(normalized[0]["success_jump"], "2")

    def test_dangling_target_id_is_cleared_not_rebound(self):
        """FR-20260811-D5-001: target_id 非空但悬空（跨方案粘贴，源 step_id 在
        当前列表不存在）时，即使旧数字分支号落在合法范围内，也**不得**改绑到
        normalized[jump-1] 指向的无关步骤——应置空并标记需用户修正。"""
        tasks = [
            {
                "step_id": "pasted_01",
                "type": 5.0,
                "value": "0",
                # 悬空：源方案指向的 step_id 不在此列表。
                "success_target_id": "source_step_999",
                # 旧数字分支号恰好落在合法范围（指向本列表第 2 步）。
                "success_jump": "2",
            },
            {"step_id": "local_02", "type": 5.0, "value": "0"},
        ]
        normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(normalized[0]["success_target_id"], "")
        self.assertEqual(normalized[0]["success_jump"], "0")

    def test_empty_target_id_still_uses_legacy_numeric_fallback(self):
        """回归守卫：target_id 为空（历史数据只有数字分支字段）时，数字回退
        绑定必须保留，不得因 D5-001 修复被误伤。"""
        tasks = [
            {"type": 5.0, "value": "0", "success_jump": "2", "success_target_id": ""},
            {"type": 5.0, "value": "0"},
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["generated_0001", "generated_0002"],
        ):
            normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(normalized[0]["success_target_id"], "generated_0002")
        self.assertEqual(normalized[0]["success_jump"], "2")

    def test_reorder_keeps_target_identity_and_refreshes_display_number(self):
        first, branch, target = self.stable_tasks()
        reordered = materialize_runtime_references([target, first, branch])
        self.assertEqual(reordered[2]["success_target_id"], "step_0003")
        self.assertEqual(reordered[2]["success_jump"], "1")

    def test_explicit_number_edit_replaces_stable_target(self):
        tasks = normalize_workflow_tasks(self.stable_tasks())
        edited = dict(tasks[1], success_jump="1")
        resolved = apply_numeric_reference_edits(edited, tasks)
        self.assertEqual(resolved["success_target_id"], "step_0001")

    def test_call_reference_tracks_target_identity_after_reorder(self):
        tasks = normalize_workflow_tasks(
            [
                {
                    "step_id": "caller_01",
                    "type": 4.0,
                    "value": "caller",
                    "call_step": "3",
                    "call_target_id": "target_03",
                },
                {"step_id": "middle_02", "type": 4.0, "value": "middle"},
                {"step_id": "target_03", "type": 4.0, "value": "target"},
            ]
        )

        reordered = materialize_runtime_references(
            [tasks[2], tasks[0], tasks[1]]
        )

        self.assertEqual(reordered[1]["call_target_id"], "target_03")
        self.assertEqual(reordered[1]["call_step"], "1")
        references = references_to_step(reordered, "target_03")
        self.assertEqual(
            [(item.source_index, item.label) for item in references],
            [(2, "先执行指定步骤")],
        )

    def test_delete_reports_and_clears_references(self):
        tasks = normalize_workflow_tasks(self.stable_tasks())
        references = references_to_step(tasks, "step_0003")
        self.assertEqual([(item.source_index, item.label) for item in references], [(2, "成功后跳至")])
        remaining = remove_task_and_clear_references(tasks, "step_0003")
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[1]["success_target_id"], "")
        self.assertEqual(remaining[1]["success_jump"], "0")

    def test_copy_rebases_self_reference_but_keeps_external_reference(self):
        task = {
            "step_id": "source_01",
            "success_target_id": "source_01",
            "fail_target_id": "external_01",
        }
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            return_value="cloned_01",
        ):
            cloned = clone_task_for_insert(task)
        self.assertEqual(cloned["step_id"], "cloned_01")
        self.assertEqual(cloned["success_target_id"], "cloned_01")
        self.assertEqual(cloned["fail_target_id"], "external_01")

    def test_independent_single_clones_use_disjoint_group_namespaces(self):
        task = {
            "step_id": "source_01",
            "type": 5.0,
            "value": "0",
            "success_jump": "1",
            "success_target_id": "source_01",
            "editor_groups": [
                {"id": "source_group", "name": "Source", "collapsed": False}
            ],
        }
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["clone_a_01", "clone_b_01"],
        ):
            first = clone_task_for_insert(task)
            second = clone_task_for_insert(task)

        first_group = first["editor_groups"][0]["id"]
        second_group = second["editor_groups"][0]["id"]
        self.assertNotEqual(first_group, second_group)
        self.assertNotIn(first_group, {"source_group", second_group})
        self.assertNotEqual(second_group, "source_group")
        self.assertEqual(first["success_target_id"], "clone_a_01")
        self.assertEqual(second["success_target_id"], "clone_b_01")

    def test_copy_batch_rebases_internal_references_and_keeps_external_ones(self):
        tasks = [
            {
                "step_id": "source_01",
                "success_target_id": "source_02",
                "fail_target_id": "external_01",
            },
            {"step_id": "source_02", "success_target_id": "source_01"},
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["cloned_01", "cloned_02"],
        ):
            cloned = clone_tasks_for_insert(tasks)
        self.assertEqual([task["step_id"] for task in cloned], ["cloned_01", "cloned_02"])
        self.assertEqual(cloned[0]["success_target_id"], "cloned_02")
        self.assertEqual(cloned[1]["success_target_id"], "cloned_01")
        self.assertEqual(cloned[0]["fail_target_id"], "external_01")

    def test_independent_batch_clones_use_disjoint_group_namespaces(self):
        group = {"id": "source_group", "name": "Source", "collapsed": False}
        tasks = [
            {
                "step_id": "source_01",
                "type": 5.0,
                "value": "0",
                "success_jump": "2",
                "success_target_id": "source_02",
                "editor_groups": [group],
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": [group],
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["clone_a_01", "clone_a_02", "clone_b_01", "clone_b_02"],
        ):
            first = clone_tasks_for_insert(tasks)
            second = clone_tasks_for_insert(tasks)

        first_group = first[0]["editor_groups"][0]["id"]
        second_group = second[0]["editor_groups"][0]["id"]
        self.assertEqual(first[1]["editor_groups"][0]["id"], first_group)
        self.assertEqual(second[1]["editor_groups"][0]["id"], second_group)
        self.assertNotEqual(first_group, second_group)
        self.assertEqual(first[0]["success_target_id"], "clone_a_02")
        self.assertEqual(second[0]["success_target_id"], "clone_b_02")

    def test_single_insert_inside_group_adopts_surrounding_group(self):
        target_group = {"id": "target_group", "name": "目标组"}
        destination = [
            {"editor_groups": [target_group]},
            {"editor_groups": [target_group]},
        ]
        cloned = [{"step_id": "clone_01", "editor_groups": []}]

        attached = attach_cloned_tasks_to_insertion_context(
            cloned, destination, 1
        )

        self.assertEqual(
            [group["id"] for group in attached[0]["editor_groups"]],
            ["target_group"],
        )
        self.assertEqual(attached[0]["editor_groups"][0]["name"], "目标组")
        self.assertEqual(cloned[0]["editor_groups"], [])

    def test_batch_insert_keeps_surrounding_group_and_nested_copy(self):
        target_group = {"id": "target_group", "name": "目标组"}
        copied_group = {"id": "copied_group", "name": "复制组"}
        destination = [
            {"editor_groups": [target_group]},
            {"editor_groups": [target_group]},
        ]
        cloned = [
            {"step_id": "clone_01", "editor_groups": [copied_group]},
            {"step_id": "clone_02", "editor_groups": [copied_group]},
        ]

        attached = attach_cloned_tasks_to_insertion_context(
            cloned, destination, 1
        )

        self.assertEqual(
            [[group["id"] for group in task["editor_groups"]] for task in attached],
            [
                ["target_group", "copied_group"],
                ["target_group", "copied_group"],
            ],
        )

    def test_insert_at_group_boundary_keeps_cloned_namespace(self):
        target_group = {"id": "target_group", "name": "目标组"}
        copied_group = {"id": "copied_group", "name": "复制组"}
        destination = [
            {"editor_groups": [target_group]},
            {"editor_groups": []},
        ]
        cloned = [{"step_id": "clone_01", "editor_groups": [copied_group]}]

        attached = attach_cloned_tasks_to_insertion_context(
            cloned, destination, 1
        )

        self.assertEqual(attached[0]["editor_groups"], [copied_group])

    def test_copy_single_group_member_detaches_every_group_level(self):
        task = {
            "step_id": "source_01",
            "editor_groups": [
                {"id": "outer", "name": "外层"},
                {"id": "middle", "name": "中层"},
                {"id": "inner", "name": "内层"},
            ],
        }
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            return_value="cloned_01",
        ):
            cloned = clone_task_for_insert(task)

        self.assertEqual(cloned["step_id"], "cloned_01")
        cloned_ids = [group["id"] for group in cloned["editor_groups"]]
        self.assertEqual(len(set(cloned_ids)), 3)
        self.assertTrue(set(cloned_ids).isdisjoint({"outer", "middle", "inner"}))
        self.assertEqual(
            [group["name"] for group in cloned["editor_groups"]],
            ["外层", "中层", "内层"],
        )

    def test_copy_single_preserves_malformed_three_levels_and_self_jump(self):
        task = {
            "step_id": "source_01",
            "type": 5.0,
            "value": "0",
            "success_jump": "1",
            "success_target_id": "source_01",
            "editor_groups": [
                {"id": "same_path", "name": "Top", "collapsed": False},
                {"id": "same_path", "name": "Inner", "collapsed": True},
                {"id": "leaf", "name": "Leaf", "collapsed": False},
            ],
        }
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            return_value="cloned_01",
        ):
            cloned = clone_task_for_insert(task)

        path = cloned["editor_groups"]
        self.assertEqual([group["name"] for group in path], ["Top", "Inner", "Leaf"])
        self.assertEqual(len({group["id"] for group in path}), 3)
        self.assertEqual(cloned["success_target_id"], "cloned_01")
        self.assertEqual(cloned["success_jump"], "1")

    def test_copy_complete_nested_group_reuses_one_new_id_per_level(self):
        outer = {"id": "outer", "name": "外层", "collapsed": False}
        middle = {"id": "middle", "name": "中层", "collapsed": True}
        inner = {"id": "inner", "name": "内层", "collapsed": False}
        tasks = [
            {"step_id": "source_01", "editor_groups": [outer]},
            {"step_id": "source_02", "editor_groups": [outer, middle]},
            {
                "step_id": "source_03",
                "editor_groups": [outer, middle, inner],
            },
            {
                "step_id": "source_04",
                "editor_groups": [outer, middle, inner],
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["cloned_01", "cloned_02", "cloned_03", "cloned_04"],
        ):
            cloned = clone_tasks_for_insert(tasks)

        outer_id = cloned[0]["editor_groups"][0]["id"]
        middle_id = cloned[1]["editor_groups"][1]["id"]
        inner_id = cloned[2]["editor_groups"][2]["id"]
        self.assertEqual(
            [[group["id"] for group in task["editor_groups"]] for task in cloned],
            [
                [outer_id],
                [outer_id, middle_id],
                [outer_id, middle_id, inner_id],
                [outer_id, middle_id, inner_id],
            ],
        )
        self.assertTrue(
            {outer_id, middle_id, inner_id}.isdisjoint(
                {"outer", "middle", "inner"}
            )
        )
        self.assertTrue(cloned[1]["editor_groups"][1]["collapsed"])

    def test_copy_batch_preserves_malformed_three_level_paths_and_jump(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        tasks = [
            {
                "step_id": "source_01",
                "type": 5.0,
                "value": "0",
                "success_jump": "2",
                "success_target_id": "source_02",
                "editor_groups": malformed,
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": malformed,
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["cloned_01", "cloned_02"],
        ):
            cloned = clone_tasks_for_insert(tasks)

        first_path = cloned[0]["editor_groups"]
        self.assertEqual([group["name"] for group in first_path], ["Top", "Inner", "Leaf"])
        self.assertEqual(len({group["id"] for group in first_path}), 3)
        self.assertEqual(first_path, cloned[1]["editor_groups"])
        self.assertEqual(cloned[0]["success_target_id"], "cloned_02")
        self.assertEqual(cloned[0]["success_jump"], "2")

    def test_delete_batch_clears_surviving_references_once(self):
        tasks = normalize_workflow_tasks(self.stable_tasks())
        remaining = remove_tasks_and_clear_references(
            tasks, {"step_0001", "step_0003"}
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["step_id"], "step_0002")
        self.assertEqual(remaining[0]["success_jump"], "0")
        self.assertEqual(remaining[0]["success_target_id"], "")

    def test_duplicate_ids_are_replaced(self):
        tasks = [
            {"step_id": "duplicate_01"},
            {"step_id": "duplicate_01"},
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            return_value="replacement_01",
        ):
            normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(normalized[0]["step_id"], "duplicate_01")
        self.assertEqual(normalized[1]["step_id"], "replacement_01")

    def test_merge_rebases_every_internal_absolute_reference(self):
        destination = [
            {"step_id": "dest_0001", "type": 5.0, "value": "0"},
            {"step_id": "dest_0002", "type": 5.0, "value": "0"},
        ]
        source = [
            {
                "step_id": "source_01",
                "type": 1.0,
                "value": "1,1",
                "success_jump": "2",
                "success_target_id": "source_02",
                "fail_jump": "1",
                "fail_target_id": "source_01",
            },
            {
                "step_id": "source_02",
                "type": 15.0,
                "value": "",
                "until_false_jump": "1",
                "until_false_target_id": "source_01",
                "until_true_jump": "2",
                "until_true_target_id": "source_02",
            },
        ]

        result = merge_workflow_tasks(destination, source)

        self.assertEqual(result.appended_count, 2)
        self.assertEqual(result.remapped_references, 4)
        self.assertEqual(len({task["step_id"] for task in result.tasks}), 4)
        self.assertEqual(result.tasks[2]["success_jump"], "4")
        self.assertEqual(result.tasks[2]["fail_jump"], "3")
        self.assertEqual(result.tasks[3]["until_false_jump"], "3")
        self.assertEqual(result.tasks[3]["until_true_jump"], "4")

    def test_merge_without_reference_preservation_clears_absolute_jumps_only(self):
        source = [
            {
                "step_id": "source_01",
                "type": 1.0,
                "value": "1,1",
                "success_skip": "2",
                "success_jump": "1",
                "success_target_id": "source_01",
            }
        ]

        result = merge_workflow_tasks(
            [], source, preserve_internal_references=False
        )

        self.assertEqual(result.cleared_references, 1)
        self.assertEqual(result.tasks[0]["success_jump"], "0")
        self.assertEqual(result.tasks[0]["success_target_id"], "")
        self.assertEqual(result.tasks[0]["success_skip"], "2")

    def test_independent_merges_use_disjoint_source_group_namespaces(self):
        group = {"id": "source_group", "name": "Source", "collapsed": False}
        source = [
            {
                "step_id": "source_01",
                "type": 5.0,
                "value": "0",
                "success_jump": "2",
                "success_target_id": "source_02",
                "editor_groups": [group],
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": [group],
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["merge_a_01", "merge_a_02", "merge_b_01", "merge_b_02"],
        ):
            first = merge_workflow_tasks([], source)
            second = merge_workflow_tasks([], source)

        first_group = first.tasks[0]["editor_groups"][0]["id"]
        second_group = second.tasks[0]["editor_groups"][0]["id"]
        self.assertEqual(first.tasks[1]["editor_groups"][0]["id"], first_group)
        self.assertEqual(second.tasks[1]["editor_groups"][0]["id"], second_group)
        self.assertNotEqual(first_group, second_group)
        self.assertEqual(first.tasks[0]["success_target_id"], "merge_a_02")
        self.assertEqual(second.tasks[0]["success_target_id"], "merge_b_02")

    def test_merge_isolates_boundary_group_ids_and_preserves_three_levels(self):
        outer = {"id": "shared_outer", "name": "同名", "collapsed": False}
        middle = {"id": "source_middle", "name": "中层", "collapsed": True}
        inner = {"id": "source_inner", "name": "内层", "collapsed": False}
        destination = [
            {
                "step_id": "dest_0001",
                "type": 1.0,
                "value": "1,1",
                "editor_groups": [outer],
            }
        ]
        source = [
            {
                "step_id": "source_01",
                "type": 5.0,
                "value": "0",
                "success_jump": "3",
                "success_target_id": "source_03",
                "editor_groups": [outer],
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": [outer, middle],
            },
            {
                "step_id": "source_03",
                "type": 1.0,
                "value": "3,3",
                "editor_groups": [outer, middle, inner],
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["merged_01", "merged_02", "merged_03"],
        ):
            result = merge_workflow_tasks(destination, source)

        self.assertEqual(result.tasks[0]["step_id"], "dest_0001")
        self.assertEqual(
            [task["step_id"] for task in result.tasks[1:]],
            ["merged_01", "merged_02", "merged_03"],
        )
        self.assertEqual(result.tasks[1]["success_target_id"], "merged_03")
        self.assertEqual(result.tasks[1]["success_jump"], "4")
        self.assertEqual(result.tasks[0]["editor_groups"][0]["id"], "shared_outer")
        merged_outer = result.tasks[1]["editor_groups"][0]["id"]
        merged_middle = result.tasks[2]["editor_groups"][1]["id"]
        merged_inner = result.tasks[3]["editor_groups"][2]["id"]
        self.assertEqual(
            [[group["id"] for group in task["editor_groups"]] for task in result.tasks[1:]],
            [
                [merged_outer],
                [merged_outer, merged_middle],
                [merged_outer, merged_middle, merged_inner],
            ],
        )
        destination_ids = {
            group["id"] for group in result.tasks[0]["editor_groups"]
        }
        source_ids = {
            group["id"]
            for task in result.tasks[1:]
            for group in task["editor_groups"]
        }
        self.assertTrue(destination_ids.isdisjoint(source_ids))

    def test_merge_workflow_with_itself_detaches_groups_and_internal_jumps(self):
        group = {"id": "self_group", "name": "自身", "collapsed": False}
        tasks = [
            {
                "step_id": "self_0001",
                "type": 5.0,
                "value": "0",
                "success_jump": "2",
                "success_target_id": "self_0002",
                "editor_groups": [group],
            },
            {
                "step_id": "self_0002",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": [group],
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["merged_01", "merged_02"],
        ):
            result = merge_workflow_tasks(tasks, tasks)

        self.assertEqual(
            [task["step_id"] for task in result.tasks],
            ["self_0001", "self_0002", "merged_01", "merged_02"],
        )
        self.assertEqual(result.tasks[2]["success_target_id"], "merged_02")
        self.assertEqual(result.tasks[2]["success_jump"], "4")
        self.assertEqual(result.tasks[0]["editor_groups"][0]["id"], "self_group")
        self.assertEqual(result.tasks[1]["editor_groups"][0]["id"], "self_group")
        copied_group_id = result.tasks[2]["editor_groups"][0]["id"]
        self.assertNotEqual(copied_group_id, "self_group")
        self.assertEqual(result.tasks[3]["editor_groups"][0]["id"], copied_group_id)

    def test_merge_preserves_source_segments_after_group_batch_remap(self):
        source_paths = [
            [
                {"id": "outer", "name": "片段一", "collapsed": False},
                {"id": "middle", "name": "中层", "collapsed": False},
                {"id": "inner", "name": "内层", "collapsed": True},
            ],
            [
                {"id": "outer", "name": "片段一", "collapsed": False},
                {"id": "middle", "name": "中层", "collapsed": False},
                {"id": "inner", "name": "内层", "collapsed": True},
            ],
            [
                {"id": "outer", "name": "片段二", "collapsed": False},
                {"id": "middle", "name": "中层", "collapsed": False},
                {"id": "inner", "name": "内层", "collapsed": True},
            ],
        ]
        source = [
            {
                "step_id": f"source_{index}",
                "type": 1.0,
                "value": f"{index},{index}",
                "editor_groups": path,
            }
            for index, path in enumerate(source_paths, start=1)
        ]

        result = merge_workflow_tasks([], source)
        remapped_paths = [
            [group["id"] for group in task["editor_groups"]]
            for task in result.tasks
        ]

        self.assertEqual(remapped_paths[0], remapped_paths[1])
        self.assertTrue(set(remapped_paths[0]).isdisjoint(remapped_paths[2]))
        self.assertTrue(set(remapped_paths[0]).isdisjoint({"outer", "middle", "inner"}))
        self.assertEqual(result.tasks[0]["editor_groups"][0]["name"], "片段一")
        self.assertEqual(result.tasks[2]["editor_groups"][0]["name"], "片段二")

    def test_merge_keeps_cross_depth_source_group_ids_isolated(self):
        top = {"id": "shared_depth", "name": "Top", "collapsed": False}
        outer = {"id": "outer_b", "name": "Outer B", "collapsed": False}
        inner = {"id": "shared_depth", "name": "Inner", "collapsed": False}
        leaf = {"id": "leaf", "name": "Leaf", "collapsed": True}
        source = [
            {
                "step_id": "source_01",
                "type": 1.0,
                "value": "1,1",
                "editor_groups": [top],
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": [outer, inner, leaf],
            },
            {
                "step_id": "source_03",
                "type": 1.0,
                "value": "3,3",
                "editor_groups": [outer, inner, leaf],
            },
        ]

        result = merge_workflow_tasks([], source)
        top_id = result.tasks[0]["editor_groups"][0]["id"]
        nested_ids = [
            group["id"] for group in result.tasks[1]["editor_groups"]
        ]

        self.assertNotEqual(top_id, nested_ids[1])
        self.assertEqual(
            result.tasks[1]["editor_groups"], result.tasks[2]["editor_groups"]
        )
        self.assertEqual(len({top_id, *nested_ids}), 4)
        self.assertTrue(
            {top_id, *nested_ids}.isdisjoint(
                {"shared_depth", "outer_b", "leaf"}
            )
        )

    def test_merge_preserves_malformed_three_level_paths_and_internal_jump(self):
        malformed = [
            {"id": "same_path", "name": "Top", "collapsed": False},
            {"id": "same_path", "name": "Inner", "collapsed": True},
            {"id": "leaf", "name": "Leaf", "collapsed": False},
        ]
        source = [
            {
                "step_id": "source_01",
                "type": 5.0,
                "value": "0",
                "success_jump": "2",
                "success_target_id": "source_02",
                "editor_groups": malformed,
            },
            {
                "step_id": "source_02",
                "type": 1.0,
                "value": "2,2",
                "editor_groups": malformed,
            },
        ]
        with mock.patch(
            "fukua_rpa.workflow_document.new_step_id",
            side_effect=["merged_01", "merged_02"],
        ):
            result = merge_workflow_tasks([], source)

        first_path = result.tasks[0]["editor_groups"]
        self.assertEqual([group["name"] for group in first_path], ["Top", "Inner", "Leaf"])
        self.assertEqual(len({group["id"] for group in first_path}), 3)
        self.assertEqual(first_path, result.tasks[1]["editor_groups"])
        self.assertEqual(result.tasks[0]["success_target_id"], "merged_02")
        self.assertEqual(result.tasks[0]["success_jump"], "2")


if __name__ == "__main__":
    unittest.main()


class MergePreservesNonApplicableDraftsTests(unittest.TestCase):
    """FR-20261005-102: 合并方案不得清零与当前动作无关的 until_* 草稿。

    合并此前把非 until 步骤的 ``until_false_jump`` / ``until_true_jump`` 强制改写为
    "0"，与 ``normalize_workflow_tasks`` 的"保留原始草稿"口径不一致；设计基准要求
    动作专属字段始终保留原始草稿。
    """

    def test_until_draft_is_preserved_on_a_non_until_step(self):
        source = [
            {
                "step_id": "src00000001",
                "type": 1.0,
                "value": "100,200",
                "until_false_jump": "3",
                "until_true_jump": "2",
                "until_false_target_id": "",
                "until_true_target_id": "",
            }
        ]
        result = merge_workflow_tasks([], source)
        merged = result.tasks[0]
        self.assertEqual(merged["until_false_jump"], "3")
        self.assertEqual(merged["until_true_jump"], "2")
        self.assertEqual(merged["until_false_target_id"], "")
        self.assertEqual(merged["until_true_target_id"], "")

    def test_until_step_references_are_still_remapped(self):
        source = [
            {
                "step_id": "src00000001",
                "type": 15.0,
                "value": "",
                "until_true_jump": "2",
                "until_true_target_id": "src00000002",
            },
            {"step_id": "src00000002", "type": 1.0, "value": "1,1"},
        ]
        result = merge_workflow_tasks([], source)
        first, second = result.tasks[0], result.tasks[1]
        self.assertEqual(first["until_true_target_id"], second["step_id"])
        self.assertEqual(first["until_true_jump"], "2")
