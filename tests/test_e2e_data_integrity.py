"""端到端数据完整性测试：覆盖核心数据路径的代表性流程。

场景 1：创建→编辑→保存→导出→导入→校验
场景 2：含跳转/循环的复杂工作流复制粘贴
场景 3：配置迁移完整性
"""

import json
import os
import tempfile
import unittest

from fukua_rpa.config_schema import (
    default_profile_config,
    migrate_profile_config,
)
from fukua_rpa.constants import PROFILE_SCHEMA_VERSION as SCHEMA_VERSION
from fukua_rpa.io_utils import atomic_write_json
from fukua_rpa.profile_structure import validate_profile_config_data
from fukua_rpa.task_model import task_data_defaults
from fukua_rpa.workflow_document import (
    clone_tasks_for_insert,
    normalize_workflow_tasks,
    valid_step_id,
)


def _make_task(command_type: float, value: str = "", **overrides) -> dict:
    """构造一个带有默认字段的步骤字典。"""
    task = task_data_defaults()
    task["type"] = command_type
    task["value"] = value
    task.update(overrides)
    return task


class TestWorkflowRoundTrip(unittest.TestCase):
    """场景 1：创建→编辑→保存→导出→导入→校验。"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = self._tmp_dir.name

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_create_edit_save_export_import_verify(self):
        # 1. 创建默认方案
        config = default_profile_config()
        self.assertEqual(config["_schema_version"], SCHEMA_VERSION)
        self.assertEqual(config["tasks"], [])

        # 2. 添加 5 个步骤（坐标点击、文本输入、等待、图片识别、跳转）
        tasks = [
            _make_task(1.0, "100,200"),           # 坐标点击
            _make_task(4.0, "hello world"),       # 文本输入
            _make_task(5.0, "1.5"),               # 等待(秒)
            _make_task(1.0, "target_image.png"),  # 图片识别（左键单击+图片目标）
            _make_task(5.0, "0.5"),               # 等待（用作跳转目标步骤）
        ]

        # 3. 设置跳转引用（步骤5成功后跳回步骤2）
        tasks[4]["success_jump"] = "2"

        # 通过 normalize 分配 step_id 并同步跳转引用
        normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(len(normalized), 5)

        # 验证所有步骤都获得了有效 step_id
        for task in normalized:
            self.assertTrue(valid_step_id(task["step_id"]))

        # 验证跳转引用：步骤5的 success_target_id 应指向步骤2的 step_id
        step2_id = normalized[1]["step_id"]
        step5 = normalized[4]
        self.assertEqual(step5["success_target_id"], step2_id)
        self.assertEqual(step5["success_jump"], "2")

        # 记录 step_id 用于后续稳定性验证
        original_ids = [t["step_id"] for t in normalized]

        # 4. 通过 atomic_write_json 保存到文件
        config["tasks"] = normalized
        save_path = os.path.join(self.tmp_path, "profile.json")
        atomic_write_json(save_path, config)
        self.assertTrue(os.path.isfile(save_path))

        # 5. 导出为 JSON（模拟导出：读取文件内容作为导出数据）
        with open(save_path, encoding="utf-8") as f:
            exported_json = f.read()
        export_path = os.path.join(self.tmp_path, "export.json")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(exported_json)

        # 6. 从导出的 JSON 重新导入
        with open(export_path, encoding="utf-8") as f:
            imported_config = json.load(f)

        # 导入后执行迁移（模拟程序加载流程）
        migration = migrate_profile_config(imported_config)
        imported = migration.value

        # 7. 验证：步骤数、跳转引用完整性、step_id 稳定性
        imported_tasks = imported["tasks"]
        self.assertEqual(len(imported_tasks), 5)

        # step_id 稳定性：导入后 ID 不变
        imported_ids = [t["step_id"] for t in imported_tasks]
        self.assertEqual(imported_ids, original_ids)

        # 跳转引用完整性：步骤5仍指向步骤2
        imported_step5 = imported_tasks[4]
        self.assertEqual(imported_step5["success_target_id"], step2_id)
        self.assertEqual(imported_step5["success_jump"], "2")

        # 验证结构合法性
        error = validate_profile_config_data(imported)
        self.assertIsNone(error)

        # 验证步骤类型和值保持不变
        self.assertEqual(float(imported_tasks[0]["type"]), 1.0)
        self.assertEqual(imported_tasks[0]["value"], "100,200")
        self.assertEqual(float(imported_tasks[1]["type"]), 4.0)
        self.assertEqual(imported_tasks[1]["value"], "hello world")
        self.assertEqual(float(imported_tasks[2]["type"]), 5.0)
        self.assertEqual(imported_tasks[2]["value"], "1.5")
        self.assertEqual(imported_tasks[3]["value"], "target_image.png")


class TestWorkflowCopyPaste(unittest.TestCase):
    """场景 2：含跳转/循环的复杂工作流复制粘贴。"""

    def test_copy_paste_preserves_references(self):
        # 1. 创建含 success/fail 分支跳转的工作流（6 个步骤）
        tasks = [
            _make_task(1.0, "10,20"),       # 步骤1：点击
            _make_task(4.0, "abc"),         # 步骤2：输入
            _make_task(5.0, "0.3"),         # 步骤3：等待
            _make_task(1.0, "50,60"),       # 步骤4：点击
            _make_task(5.0, "1.0"),         # 步骤5：等待
            _make_task(1.0, "70,80"),       # 步骤6：点击
        ]

        # 设置内部跳转：步骤2成功跳到步骤4，步骤4失败跳到步骤2
        tasks[1]["success_jump"] = "4"
        tasks[3]["fail_jump"] = "2"

        # 规范化以分配 step_id 并解析跳转
        normalized = normalize_workflow_tasks(tasks)
        self.assertEqual(len(normalized), 6)

        # 验证原始跳转已正确建立
        step2_id = normalized[1]["step_id"]
        step4_id = normalized[3]["step_id"]
        self.assertEqual(normalized[1]["success_target_id"], step4_id)
        self.assertEqual(normalized[3]["fail_target_id"], step2_id)

        # 2. 复制步骤 2-4（索引 1-3，含内部跳转引用）
        source_batch = normalized[1:4]
        self.assertEqual(len(source_batch), 3)

        cloned = clone_tasks_for_insert(source_batch)
        self.assertEqual(len(cloned), 3)

        # 3. 粘贴到步骤 6 之后（模拟插入到末尾）
        combined = normalized + cloned
        result = normalize_workflow_tasks(combined)
        self.assertEqual(len(result), 9)

        # 4. 验证：新步骤有独立 step_id
        original_ids = {t["step_id"] for t in normalized}
        cloned_ids = {t["step_id"] for t in result[6:]}

        # 克隆步骤的 ID 与原始步骤完全不同
        self.assertEqual(len(cloned_ids), 3)
        self.assertEqual(original_ids & cloned_ids, set())

        # 所有 9 个步骤的 ID 互不相同
        all_ids = [t["step_id"] for t in result]
        self.assertEqual(len(set(all_ids)), 9)

        # 验证：跳转引用正确更新
        # 克隆的步骤2（索引6）的 success 应指向克隆的步骤4（索引8）
        cloned_step2 = result[6]
        cloned_step4 = result[8]
        self.assertEqual(cloned_step2["success_target_id"], cloned_step4["step_id"])

        # 克隆的步骤4（索引8）的 fail 应指向克隆的步骤2（索引6）
        self.assertEqual(cloned_step4["fail_target_id"], cloned_step2["step_id"])

        # 原始步骤的跳转引用不受影响
        self.assertEqual(result[1]["success_target_id"], result[3]["step_id"])
        self.assertEqual(result[3]["fail_target_id"], result[1]["step_id"])

        # 验证跳转数字也已同步
        # 克隆步骤2的 success_jump 应指向克隆步骤4的显示编号（第9步）
        self.assertEqual(cloned_step2["success_jump"], "9")
        # 克隆步骤4的 fail_jump 应指向克隆步骤2的显示编号（第7步）
        self.assertEqual(cloned_step4["fail_jump"], "7")


class TestConfigMigration(unittest.TestCase):
    """场景 3：配置迁移完整性。"""

    def test_migration_preserves_all_fields(self):
        # 1. 构造一个旧版本格式的配置字典（缺少新字段）
        old_config = {
            "_schema_version": 0,
            "conf": "0.9",
            "scale_min": "0.7",
            "scale_max": "1.3",
            "gray_en": False,
            "move_spd": "5.0",
            "click_hld": "0.08",
            "settle": "1.0",
            "timeout": "3.0",
            "hotkey_start": "F5",
            "hotkey_stop": "F6",
            "loop_mode": "循环",
            "loop_val": "20",
            "multi_target_mode": "最佳一个",
            "scan_region": "0,0,100,100",
            "log_level": 2,
            "tasks": [
                {
                    "type": "1",
                    "value": "200,300",
                    "success_jump": "0",
                    "fail_jump": "0",
                },
                {
                    "type": "5",
                    "value": "2.0",
                    "success_jump": "1",
                    "fail_jump": "0",
                },
            ],
        }

        # 2. 调用 migrate_profile_config
        result = migrate_profile_config(old_config)
        migrated = result.value

        # 验证迁移确实发生了
        self.assertTrue(result.changed)
        self.assertEqual(result.source_version, 0)
        self.assertEqual(result.target_version, SCHEMA_VERSION)

        # 3. 验证：旧字段值不变
        self.assertEqual(migrated["conf"], "0.9")
        self.assertEqual(migrated["scale_min"], "0.7")
        self.assertEqual(migrated["scale_max"], "1.3")
        self.assertFalse(migrated["gray_en"])
        self.assertEqual(migrated["move_spd"], "5.0")
        self.assertEqual(migrated["click_hld"], "0.08")
        self.assertEqual(migrated["settle"], "1.0")
        self.assertEqual(migrated["timeout"], "3.0")
        self.assertEqual(migrated["hotkey_start"], "F5")
        self.assertEqual(migrated["hotkey_stop"], "F6")
        self.assertEqual(migrated["loop_mode"], "循环")
        self.assertEqual(migrated["loop_val"], "20")

        # v0 迁移：multi_target_mode "最佳一个" → "快速一个"
        self.assertEqual(migrated["multi_target_mode"], "快速一个")

        # v0 迁移：scan_region 存在时，scan_regions 应包含它
        self.assertEqual(migrated["scan_regions"], ["0,0,100,100"])

        # 验证：所有新字段有默认值
        defaults = default_profile_config()
        for key in defaults:
            self.assertIn(key, migrated, f"迁移后缺少字段：{key}")

        # 新增字段应等于默认值（旧配置中不存在的字段）
        new_fields = [
            "native_core_en", "native_parallel_mode", "isolated_runner_en",
            "scale_memory_tier", "dodge_en", "dbl_dodge",
            "adaptive_backoff", "scene_wake_en", "playback_speed",
            "step_adjust_follow", "sound_alert_audio_path",
            "sound_alert_play_seconds", "cpu_refresh_interval",
            "ui_scale", "theme_mode",
        ]
        for key in new_fields:
            self.assertEqual(
                migrated[key], defaults[key],
                f"新字段 {key} 应为默认值 {defaults[key]!r}，"
                f"实际为 {migrated[key]!r}",
            )

        # 4. 验证：data_version 已更新
        self.assertEqual(migrated["_schema_version"], SCHEMA_VERSION)

        # 验证：tasks 被规范化（type 从字符串转为浮点数，step_id 已分配）
        self.assertEqual(len(migrated["tasks"]), 2)
        for task in migrated["tasks"]:
            self.assertIsInstance(task["type"], float)
            self.assertTrue(valid_step_id(task["step_id"]))

        # 验证步骤1：success_jump="0" 表示无跳转
        task1 = migrated["tasks"][0]
        self.assertEqual(task1["success_target_id"], "")
        self.assertEqual(task1["success_jump"], "0")

        # 验证步骤2的跳转引用：success_jump="1" 指向步骤1
        task2 = migrated["tasks"][1]
        self.assertEqual(task2["success_target_id"], task1["step_id"])
        self.assertEqual(task2["success_jump"], "1")

        # 验证迁移后的配置通过结构校验
        error = validate_profile_config_data(migrated)
        self.assertIsNone(error)

    def test_migration_idempotent(self):
        """迁移已处于最新版本的配置不应产生变化。"""
        config = default_profile_config()
        config["tasks"] = normalize_workflow_tasks([
            _make_task(1.0, "10,20"),
            _make_task(5.0, "1.0"),
        ])
        result = migrate_profile_config(config)
        self.assertFalse(result.changed)
        self.assertEqual(result.source_version, SCHEMA_VERSION)
        self.assertEqual(result.value, config)

    def test_migration_rejects_newer_version(self):
        """高于当前版本的配置应被拒绝。"""
        from fukua_rpa.config_schema import UnsupportedProfileVersion

        future_config = default_profile_config()
        future_config["_schema_version"] = SCHEMA_VERSION + 1
        with self.assertRaises(UnsupportedProfileVersion):
            migrate_profile_config(future_config)


if __name__ == "__main__":
    unittest.main()
