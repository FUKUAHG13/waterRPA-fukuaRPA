# pyright: reportAttributeAccessIssue=false
"""Import/export Mixin for ScheduledTasksWindow (God Class split phase 1).

Groups the JSON import / export actions. Delegates to ``self._service`` and
uses ``self._selected_schedule_ids`` provided by the window. No state of its own.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ...io_utils import atomic_write_json


class ImportExportMixin:
    """Plan import / export actions (2 methods)."""

    def _on_import(self) -> None:
        if self._deny_while_locked():  # UIC15：运行中禁止导入计划
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入计划", "", "JSON 文件 (*.json);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            # FR-20260812-D13-002: 用 load_bounded_json_file 读取前预检大小，
            # 避免先无界 read() 再查上限造成超大文件瞬时内存尖峰。
            from ...json_safety import load_bounded_json_file

            data = load_bounded_json_file(
                path,
                max_bytes=10 * 1024 * 1024,
                label="计划任务导入",
            )
        except Exception as exc:  # noqa: BLE001 - surface to the user
            QMessageBox.critical(self, "导入失败", f"无法读取文件：{exc}")
            return

        if isinstance(data, dict):
            dicts = data["tasks"] if isinstance(data.get("tasks"), list) else [data]
        elif isinstance(data, list):
            dicts = data
        else:
            QMessageBox.critical(self, "导入失败", "文件格式无法识别。")
            return

        try:
            # FR-20260812 建议-体验-3: 收集跳过明细并在成功提示中披露。
            skipped: list[str] = []
            imported = self._service.import_tasks(
                dicts, default_disabled=True, report_errors=skipped
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", f"计划数据无效：{exc}")
            return
        msg = f"已导入 {len(imported)} 个计划（全部默认停用，请检查后手动启用）。"
        if skipped:
            msg += f"\n\n有 {len(skipped)} 条记录格式错误被跳过：\n"
            msg += "\n".join(skipped[:5])
            if len(skipped) > 5:
                msg += f"\n…（其余 {len(skipped) - 5} 条略）"
        profile_names: set = set()
        for d in dicts:
            pname = d.get("profile_name_snapshot", "") or d.get("profile_id", "")
            if pname:
                profile_names.add(pname)
        if profile_names:
            msg += f"\n\n涉及方案：{'、'.join(sorted(profile_names))}"
            msg += "\n启用前请确认对应方案存在且配置有效。"
        QMessageBox.information(self, "导入完成", msg)

    def _on_export(self) -> None:
        ids = self._selected_schedule_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择要导出的计划。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出所选计划", "scheduled_tasks_export.json",
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        dicts = []
        for sid in ids:
            d = self._service.export_task(sid)
            if d:
                dicts.append(d)
        try:
            atomic_write_json(path, dicts, indent=2, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"无法写入文件：{exc}")
            return
        QMessageBox.information(self, "导出完成", f"已导出 {len(dicts)} 个计划定义。")
