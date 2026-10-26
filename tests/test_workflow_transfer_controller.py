"""FR-SCOPE005-001：普通导出必须与导入侧使用同一结构校验。

权威缺陷（`05_权威报告.md` / `06_改进路线.md` 阶段 3）：普通导出路径
`WorkflowTransferController.save()` 只调用 `atomic_write_json(path, data)`，
**未**调用结构校验；而同一文件里的批量集合导出（`export_collection_package`）与
全量包导出（`export_full_package`）都已调用。结果是用户可以导出一个"导出成功但
本版本无法导入"的方案文件（实测 5001 个步骤超过 `MAX_TASKS_PER_PROFILE=5000`），
且导出时没有任何警告。

本测试固定两条判据：

* 超限方案导出被**明确拒绝**，且不写出文件（修复前：文件被写出、无警告）；
* 正常规模与边界规模（恰好等于上限）的导出继续成功，且写出的文件能通过
  导入侧**同一序列**的结构校验（修复前即已成立，用于防止过度拒绝）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QMessageBox

from fukua_rpa.config_schema import default_profile_config, migrate_profile_config
from fukua_rpa.constants import MAX_TASKS_PER_PROFILE
from fukua_rpa.json_safety import load_bounded_json_file
from fukua_rpa.profile_package import parse_profile_collection_payload
from fukua_rpa.profile_structure import validate_profile_config_data
from fukua_rpa.ui.controllers.workflow_transfer_controller import (
    WorkflowTransferController,
)

SAVE_DIALOG = "fukua_rpa.ui.controllers.workflow_transfer_controller.QFileDialog.getSaveFileName"


class _ProfileControllerStub:
    """只暴露 `save()` 用到的校验入口，且委托给导入侧同一个函数。"""

    @staticmethod
    def validate_profile_config_data(cfg, label="方案"):
        return validate_profile_config_data(cfg, label)


class _StubHost:
    """`save()` 所需的最小宿主表面（协议见 `ui/controllers/host_view.py`）。"""

    def __init__(self, config, name="测试方案"):
        self.base_dir = ""
        self.current_profile_name = name
        self.config_path = ""
        self.profile_controller = _ProfileControllerStub()
        self.application_logs: list[str] = []
        self._config = config
        self.profiles_data = {name: config}

    def get_current_ui_config(self):
        return self._config

    def append_application_log(self, msg, *_args, **_kwargs):
        self.application_logs.append(msg)


class PlainExportValidationTests(unittest.TestCase):
    """`WorkflowTransferController.save()` 的导出前结构校验。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="fr005-export-")
        self.addCleanup(shutil.rmtree, self._tmp_dir, True)

    def _export(self, config):
        """调用 `save()`；返回 (目标路径, 宿主, QMessageBox.warning 的 mock)。"""
        host = _StubHost(config)
        controller = WorkflowTransferController(host)
        destination = os.path.join(self._tmp_dir, "exported.json")
        with mock.patch(SAVE_DIALOG, return_value=(destination, "JSON (*.json)")), \
             mock.patch.object(QMessageBox, "warning") as warning:
            controller.save()
        return destination, host, warning

    def _tasks(self, count):
        return [{"type": 1.0, "value": ""} for _ in range(count)]

    # -- 判据①：超限导出被明确拒绝 ----------------------------------------

    def test_export_rejects_more_tasks_than_limit(self):
        """5001 个步骤的方案不得被写出，且必须给出明确警告。

        修复前：`atomic_write_json` 直接落盘，`QMessageBox.warning` 从不被调用。
        """
        config = default_profile_config()
        config["tasks"] = self._tasks(MAX_TASKS_PER_PROFILE + 1)

        destination, host, warning = self._export(config)

        self.assertFalse(
            os.path.exists(destination),
            "超限方案不得被写出：该文件在本版本无法导入",
        )
        self.assertTrue(
            warning.called, "超限导出必须明确报错，而不是静默生成残缺文件"
        )
        message = str(warning.call_args[0][2])
        self.assertIn(str(MAX_TASKS_PER_PROFILE + 1), message)
        self.assertIn(str(MAX_TASKS_PER_PROFILE), message)
        self.assertEqual(
            [log for log in host.application_logs if "成功导出" in log],
            [],
            "被拒绝的导出不得记录成功日志",
        )

    # -- 判据②：正常规模与边界规模继续成功且可导入 -------------------------

    def test_export_round_trips_at_normal_scale(self):
        config = default_profile_config()
        config["tasks"] = self._tasks(3)

        destination, _host, warning = self._export(config)

        self.assertFalse(warning.called)
        self.assertTrue(os.path.isfile(destination))
        # 导入侧对 JSON 对象的处理序列：load_bounded_json_file ->
        # migrate_profile_config -> validate_profile_config_data。
        data = load_bounded_json_file(destination, max_bytes=128 * 1024 * 1024, label="导入方案")
        self.assertIsNone(
            validate_profile_config_data(
                migrate_profile_config(data).value, "方案“测试方案”"
            ),
            "导出的文件必须能通过导入侧的同一结构校验",
        )
        with open(destination, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["tasks"]), 3)

    def test_export_accepts_exactly_the_task_limit(self):
        """边界规模不得被过度拒绝（上限是 5000，5000 本身合法）。"""
        config = default_profile_config()
        config["tasks"] = self._tasks(MAX_TASKS_PER_PROFILE)

        destination, _host, warning = self._export(config)

        self.assertFalse(warning.called)
        self.assertTrue(os.path.isfile(destination))

    def test_export_accepts_legacy_profile_without_explicit_schema(self):
        """旧版方案（缺少新版默认字段）仍须可导出。

        导入侧会先 `migrate_profile_config` 再校验；导出侧若直接校验**未迁移**
        的对象，会把本可导入的旧方案误判为超限（顶层字段数口径不同），
        构成过度拒绝。
        """
        config = default_profile_config()
        config["tasks"] = self._tasks(2)
        # 模拟旧方案：移除若干新版默认字段，使其看起来"顶层字段更少但结构更旧"。
        for key in ("log_custom_categories", "cpu_refresh_interval"):
            config.pop(key, None)

        destination, _host, warning = self._export(config)

        self.assertFalse(
            warning.called, f"旧方案被过度拒绝：{warning.call_args if warning.called else ''}"
        )
        self.assertTrue(os.path.isfile(destination))


class BatchCollectionExportValidationTests(unittest.TestCase):
    """FR-20260930-016：集合 JSON 导出必须与导入侧使用同一结构校验。

    权威缺陷（`05_权威报告.md` / `06_改进路线.md` 阶段 B）：`save_batch()` 的
    ``export_type == "json"`` 分支只做总大小检查即写盘，**未**调用
    ``validate_profile_config_data``；同一分支的 ZIP 分支与 ``export_full_package``
    都已调用。结果是用户可以得到一份"导出成功但本版本无法导入"的方案集合。

    判据与 `PlainExportValidationTests` 对称：超限集合导出被明确拒绝且不落盘；
    正常与边界规模继续成功，且写出的文件能按导入侧序列重新解析并逐条校验。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="fr016-batch-export-")
        self.addCleanup(shutil.rmtree, self._tmp_dir, True)

    def _export(self, config):
        """调用 `save_batch()` 的 JSON 分支；返回 (目标路径, 宿主, warning mock)。"""
        host = _StubHost(config)
        controller = WorkflowTransferController(host)
        destination = os.path.join(self._tmp_dir, "collection.json")
        with mock.patch.object(
            WorkflowTransferController,
            "_choose_batch_export",
            return_value=(["测试方案"], "json"),
        ), mock.patch(SAVE_DIALOG, return_value=(destination, "JSON (*.json)")), \
                mock.patch.object(QMessageBox, "warning") as warning:
            controller.save_batch()
        return destination, host, warning

    def _tasks(self, count):
        return [{"type": 1.0, "value": ""} for _ in range(count)]

    def test_batch_json_export_rejects_over_limit_profile(self):
        """5001 步的集合 JSON 导出必须被拒绝且不写出文件。

        修复前：JSON 分支直接 `atomic_write_json`，文件被写出且无警告。
        """
        config = default_profile_config()
        config["tasks"] = self._tasks(MAX_TASKS_PER_PROFILE + 1)

        destination, host, warning = self._export(config)

        self.assertFalse(
            os.path.exists(destination),
            "超限方案集合不得被写出：该文件在本版本无法导入",
        )
        self.assertTrue(
            warning.called, "超限集合导出必须明确报错，而不是静默生成残缺文件"
        )
        message = str(warning.call_args[0][2])
        self.assertIn(str(MAX_TASKS_PER_PROFILE + 1), message)
        self.assertIn(str(MAX_TASKS_PER_PROFILE), message)
        self.assertEqual(
            [log for log in host.application_logs if "批量导出" in log],
            [],
            "被拒绝的批量导出不得记录成功日志",
        )

    def test_batch_json_export_round_trips_at_normal_scale(self):
        """正常规模集合 JSON 必须写出，且能被导入侧序列重新解析并逐条校验。"""
        config = default_profile_config()
        config["tasks"] = self._tasks(3)

        destination, _host, warning = self._export(config)

        self.assertFalse(warning.called)
        self.assertTrue(os.path.isfile(destination))
        data = load_bounded_json_file(
            destination, max_bytes=128 * 1024 * 1024, label="导入方案集合"
        )
        entries = parse_profile_collection_payload(data)
        self.assertIsNotNone(entries, "写出的集合 JSON 必须能被导入侧解析")
        self.assertEqual(len(entries), 1)
        name, profile = entries[0]
        self.assertEqual(name, "测试方案")
        self.assertIsNone(
            validate_profile_config_data(profile, f"方案“{name}”"),
            "集合中每个方案都必须通过导入侧的同一结构校验",
        )

    def test_batch_json_export_accepts_exactly_the_task_limit(self):
        """边界规模不得被过度拒绝（上限是 5000，5000 本身合法）。"""
        config = default_profile_config()
        config["tasks"] = self._tasks(MAX_TASKS_PER_PROFILE)

        destination, _host, warning = self._export(config)

        self.assertFalse(warning.called)
        self.assertTrue(os.path.isfile(destination))


class CollectionImportNodeBoundTests(unittest.TestCase):
    """FR-20261011-006（提示词 9 返修）：UI「导入方案」链路必须与导出侧同一节点口径。

    B 在 `08_修复复验.md` 指出：本轮只修了 `config_store` / `profile_package` 的
    集合级加载，UI 的「导入方案」JSON 分支（`WorkflowTransferController.load`）仍以
    默认 2M 节点校验整份集合正文，而导出侧只校验聚合字节。于是"导出成功但本版本
    无法导入"的方案集合仍存在。

    判据与 `docs/设计基准.md` DOC-SCOPE005-001 一致：导出通过的集合必须能被同版本
    导入。本测试构造一份聚合节点数超过默认 2M、但字节仍在导出上限内的集合，断言
    导入不被拒绝，并断言该文件确实超过默认节点口径（否则本用例不构成原缺陷场景）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="fr006-import-")
        self.addCleanup(shutil.rmtree, self._tmp_dir, True)

    def _collection_payload(self):
        """两个方案，各约 1.05M 节点：逐方案低于默认上限，聚合超过默认上限。"""
        from fukua_rpa.constants import PROFILE_COLLECTION_FORMAT
        from fukua_rpa.json_safety import DEFAULT_MAX_NODES

        filler = [0] * (DEFAULT_MAX_NODES // 2 + 1000)

        def profile():
            cfg = default_profile_config()
            cfg["tasks"] = [{"type": 1.0, "value": list(filler), "step_id": "s1"}]
            return cfg

        return {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": [
                {"name": "集合A", "profile": profile()},
                {"name": "集合B", "profile": profile()},
            ],
        }

    def test_ui_import_accepts_collection_within_export_byte_cap(self):
        from fukua_rpa.constants import MAX_PROFILE_STORE_JSON_BYTES
        from fukua_rpa.json_safety import loads_bounded_json

        payload = self._collection_payload()
        text = json.dumps(payload, ensure_ascii=False)
        # 前置①：这份集合确在导出侧字节上限内——导出会成功。
        self.assertLessEqual(len(text.encode("utf-8")), MAX_PROFILE_STORE_JSON_BYTES)
        # 前置②：它确会超过默认 2M 节点口径——修复前导入会被拒。
        with self.assertRaises(ValueError):
            loads_bounded_json(
                text, max_bytes=MAX_PROFILE_STORE_JSON_BYTES, label="导入方案"
            )

        path = os.path.join(self._tmp_dir, "collection.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

        host = _StubHost(default_profile_config())
        controller = WorkflowTransferController(host)
        with mock.patch(
            "fukua_rpa.ui.controllers.workflow_transfer_controller.QFileDialog.getOpenFileName",
            return_value=(path, "JSON (*.json)"),
        ), mock.patch(
            "fukua_rpa.ui.controllers.workflow_transfer_controller.validate_profile_for_ui",
            side_effect=lambda profile: profile,
        ), mock.patch.object(
            WorkflowTransferController, "review_imported_workflows", return_value=None
        ) as review, mock.patch.object(QMessageBox, "warning") as warning:
            controller.load()

        self.assertFalse(
            warning.called,
            "本版本可导出的集合必须可导入；实际被拒："
            f"{warning.call_args if warning.called else ''}",
        )
        self.assertTrue(review.called, "集合导入必须进入复核流程")
        entries = review.call_args[0][0]
        self.assertEqual([name for name, _ in entries], ["集合A", "集合B"])


if __name__ == "__main__":
    unittest.main()
