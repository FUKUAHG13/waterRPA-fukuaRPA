# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Bidirectional binding between global settings controls and profiles."""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from ...config_schema import default_profile_config
from ...constants import DEFAULT_PROFILE_BACKUP_COUNT
from ...credentials import CredentialStoreError
from ...log_policy import LOG_PARAMETERS
from ...vision.scale_memory import (
    ScaleMemoryPolicy,
    format_manual_scales,
    parse_manual_scales,
)
from ...workflow_summary import task_drag_summary
from ..settings_binding import (
    apply_global_settings,
    apply_settings_controls,
    capture_global_settings,
)

if TYPE_CHECKING:
    from .host_view import HostView


logger = logging.getLogger(__name__)


class SettingsController:
    """Capture and apply profile settings without owning the main window."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self._settings_audit_snapshot = None

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("设置界面已销毁")
        return host

    def default_config(self):
        return default_profile_config()

    def advanced_settings_are_non_default(self):
        host = self.host
        return any(
            (
                not host.native_core_chk.isChecked(),
                host.native_parallel_combo.currentData() != "auto",
                not host.native_scale_hint_chk.isChecked(),
                host.isolated_runner_chk.isChecked(),
                host.scale_memory_tier_combo.currentData() != "balanced",
                bool(host.scale_memory_manual_edit.text().strip()),
                host.scale_memory_custom_chk.isChecked(),
                not host.scene_wake_chk.isChecked(),
                host.scene_wake_sensitivity_combo.currentData() != "balanced",
                host.cpu_refresh_combo.currentData() != 2000,
                host.dodge_chk.isChecked(),
                host.multi_mode_combo.currentText() != "快速一个",
                host.start_step_edit.text().strip() != "1",
                host.loop_start_round_edit.text().strip() != "1",
                host.loop_end_round_edit.text().strip() not in ("", "0"),
                host.timeout.text().strip() not in ("", "0", "0.0"),
                host.timeout_stop_chk.isChecked(),
                host.move_spd.text().strip() not in ("", "0", "0.0"),
                host.click_hld.text().strip() not in ("", "0.04"),
                host.move_jitter.text().strip() not in ("", "0", "0.0"),
                host.click_hld_jitter.text().strip() not in ("", "0", "0.0"),
                host.log_level_combo.currentData() != "simple",
                host.log_file_chk.isChecked(),
                not host.log_ui_chk.isChecked(),
                not host.log_privacy_chk.isChecked(),
                not host.risk_tip_chk.isChecked(),
                not host.first_use_notices_chk.isChecked(),
                host.profile_hotkey_mode_chk.isChecked(),
                host.profile_backup_count_spin.value()
                != DEFAULT_PROFILE_BACKUP_COUNT,
            )
        )

    def current_mode(self):
        combo = getattr(self.host, "settings_mode_combo", None)
        if combo is not None:
            return "advanced" if combo.currentData() == "advanced" else "simple"
        # C-1：设置对话框延迟构建，combo 在首开前不存在。此时必须以**持久化模式**
        # 为准，否则高级用户会被静默降级为简易模式（调试步骤被过滤、断点失效）。
        return str(self.host.settings.value("settings_view_mode", "simple") or "simple")

    def tasks_for_current_mode(self, tasks):
        """Hide debug-only behavior in simple mode without mutating profiles.

        Advanced mode returns the given list unchanged (no copy). Simple mode
        only copies and clears debug_breakpoint when at least one task actually
        carries a truthy breakpoint, avoiding an unconditional full deep copy.
        Callers pass a fresh snapshot_tasks() copy, so returning the input
        directly does not alias persistent state.

        DESIGN: this breakpoint suppression is the sole execution-semantic
        exception between simple and advanced views. Other applicable advanced
        settings remain active when hidden. See ``docs/设计基准.md``.
        """
        if self.current_mode() == "advanced":
            return tasks
        if not any(
            isinstance(task, dict) and task.get("debug_breakpoint")
            for task in tasks
        ):
            return tasks
        prepared = copy.deepcopy(tasks)
        for task in prepared:
            if isinstance(task, dict):
                task["debug_breakpoint"] = False
        return prepared

    def update_debug_ui_visibility(self):
        host = self.host
        advanced = self.current_mode() == "advanced"
        paused = bool(getattr(host, "debug_paused_step", 0))
        breakpoint_button = getattr(host, "breakpoint_toggle_btn", None)
        if breakpoint_button is not None:
            breakpoint_button.setVisible(advanced)
        toolbar = getattr(host, "debug_toolbar_widget", None)
        if toolbar is not None:
            toolbar.setVisible(advanced or paused)

    def refresh_advanced_settings_notice(self):
        host = self.host
        notice = getattr(host, "advanced_settings_notice", None)
        if notice is None:
            return
        advanced = self.current_mode() == "advanced"
        privacy_disabled = bool(
            getattr(host, "log_privacy_chk", None)
            and not host.log_privacy_chk.isChecked()
        )
        if advanced:
            notice.setText("")
        elif privacy_disabled:
            notice.setText("日志隐私保护已关闭")
        elif self.advanced_settings_are_non_default():
            notice.setText("部分高级设置仍在生效")
        else:
            notice.setText("")
        notice.setToolTip(
            "切换到高级模式可查看；简易模式不会重置或停用这些设置。"
            + (
                " 当前日志可能包含普通输入、变量值、窗口条件、完整路径和错误详情。"
                if privacy_disabled
                else ""
            )
        )

    def apply_mode(self, _index=None):
        host = self.host
        combo = getattr(host, "settings_mode_combo", None)
        if combo is not None:
            advanced = combo.currentData() == "advanced"
        else:
            # C-1：combo 在首开前不存在，按持久化模式应用（与 current_mode 同源）。
            advanced = str(
                host.settings.value("settings_view_mode", "simple") or "simple"
            ) == "advanced"
        mode = "advanced" if advanced else "simple"
        previous_mode = str(
            host.settings.value("settings_view_mode", "simple") or "simple"
        )

        host.settings.setValue(
            "settings_view_mode", mode
        )
        for widget in getattr(host, "advanced_setting_widgets", []):
            widget.setVisible(advanced)
        task_list = getattr(host, "task_list", None)
        if task_list is not None:
            for row_index in range(task_list.count()):
                row = task_list.itemWidget(task_list.item(row_index))
                if row is not None and hasattr(row, "set_settings_mode"):
                    row.set_settings_mode(mode)
        for dialog in list(getattr(host, "task_config_dialogs", set())):
            try:
                if hasattr(dialog, "apply_settings_mode"):
                    dialog.apply_settings_mode(mode)
            except RuntimeError:
                host.task_config_dialogs.discard(dialog)
        self.refresh_advanced_settings_notice()
        if task_list is not None:
            for row_index in range(task_list.count()):
                item = task_list.item(row_index)
                row = task_list.itemWidget(item)
                data = row.get_data() if row is not None else item.data(Qt.UserRole)
                if isinstance(data, dict):
                    item.setData(
                        Qt.UserRole + 1,
                        (
                            row.drag_summary()
                            if row is not None
                            else task_drag_summary(data, row_index + 1, mode)
                        ),
                    )
            host.workflow_editor.refresh_editor_visibility()
        self.update_debug_ui_visibility()
        performance_controller = getattr(
            host, "window_performance_controller", None
        )
        if performance_controller is not None:
            performance_controller.refresh_resource_label_for_mode()
        if previous_mode != mode and not host.is_switching_profile:
            host.log_setting_change(
                "设置视图", "高级模式" if advanced else "简易模式"
            )

    def _capture_settings_audit_snapshot(self):
        snapshot = self.capture()
        if not isinstance(snapshot, dict):
            return {}
        # Steps have their own edit audit and may contain secret-text references.
        snapshot.pop("tasks", None)
        # FR-20260809-0908: 先移除 tasks 再深拷贝——修复前对含完整步骤列表的
        # 配置 deepcopy 是无谓开销（tasks 随即被丢弃；步骤列表可含大量步骤）。
        snapshot = copy.deepcopy(snapshot)
        return snapshot

    def on_dialog_visibility_changed(self, visible):
        host = self.host
        if visible:
            self._settings_audit_snapshot = (
                str(getattr(host, "current_profile_name", "")),
                self._capture_settings_audit_snapshot(),
            )
            return

        previous = self._settings_audit_snapshot
        self._settings_audit_snapshot = None
        if previous is None or host.is_switching_profile:
            return
        profile_name, before = previous
        if profile_name != str(getattr(host, "current_profile_name", "")):
            return
        after = self._capture_settings_audit_snapshot()
        changed = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        if not changed:
            return

        public_message = (
            f"全局设置会话已保存 {len(changed)} 项变化；字段："
            + "、".join(changed)
            + "。"
        )
        private_parts = []
        for key in changed:
            old_value = json.dumps(before.get(key), ensure_ascii=False)
            new_value = json.dumps(after.get(key), ensure_ascii=False)
            private_parts.append(
                f"{key}: {old_value[:240]} -> {new_value[:240]}"
            )
        host.append_application_log(
            public_message,
            LOG_PARAMETERS,
            private_msg=public_message + " " + "；".join(private_parts),
        )

    def capture(self):
        return capture_global_settings(self.host)

    def apply(self, cfg):
        return apply_global_settings(self.host, cfg)

    def apply_controls_only(self, cfg):
        """Apply settings controls without rebuilding the step list (FR-20261022-201)."""
        return apply_settings_controls(self.host, cfg)

    # ------------------------------------------------------------------
    # Credential management (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def refresh_credential_names(self, selected=""):
        host = self.host
        combo = getattr(host, "credential_combo", None)
        if combo is None:
            return
        current = str(selected or combo.currentText()).strip()
        try:
            names = host.credentials.names()
        except CredentialStoreError as error:
            names = []
            QMessageBox.warning(host, "凭据库读取失败", str(error))
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        if current in names:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def edit_credential(self):
        host = self.host
        current = str(host.credential_combo.currentText()).strip()
        name, accepted = QInputDialog.getText(
            host,
            "新增或修改凭据",
            "凭据名称（脚本步骤中填写此名称）:",
            QLineEdit.Normal,
            current,
        )
        if not accepted:
            return
        secret, accepted = QInputDialog.getText(
            host,
            "输入秘密内容",
            "秘密内容将使用当前 Windows 用户的 DPAPI 加密保存:",
            QLineEdit.Password,
        )
        if not accepted:
            return
        try:
            host.credentials.set(name, secret)
        except CredentialStoreError as error:
            QMessageBox.warning(host, "保存凭据失败", str(error))
            return
        self.refresh_credential_names(str(name).strip())
        QMessageBox.information(
            host,
            "凭据已保存",
            "凭据已加密保存。脚本方案只会记录凭据名称。",
        )

    def delete_credential(self):
        host = self.host
        name = str(host.credential_combo.currentText()).strip()
        if not name:
            return
        answer = QMessageBox.question(
            host,
            "删除凭据",
            f"确定删除凭据“{name}”吗？引用它的脚本步骤之后会执行失败。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            host.credentials.delete(name)
        except CredentialStoreError as error:
            QMessageBox.warning(host, "删除凭据失败", str(error))
            return
        self.refresh_credential_names()

    def open_config_dir(self):
        host = self.host
        try:
            config_dir = os.path.normpath(host.base_dir)
            if not os.path.isdir(config_dir):
                config_dir = os.path.dirname(os.path.abspath(host.config_path))
            subprocess.Popen(["explorer.exe", config_dir])
        except Exception as e:
            QMessageBox.warning(host, "错误", f"无法打开配置目录: {e}")

    # ------------------------------------------------------------------
    # Scale memory (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def current_scale_memory_policy(self):
        host = self.host
        try:
            manual_scales = parse_manual_scales(
                host.scale_memory_manual_edit.text()
            )
        except ValueError:
            manual_scales = ()
        return ScaleMemoryPolicy(
            enabled=host.native_scale_hint_chk.isChecked(),
            tier=host.scale_memory_tier_combo.currentData() or "balanced",
            manual_scales=manual_scales,
            custom_enabled=host.scale_memory_custom_chk.isChecked(),
            preferred_limit=host.scale_memory_preferred_spin.value(),
            history_limit=host.scale_memory_history_spin.value(),
        ).normalized()

    def refresh_scale_memory_status(self):
        host = self.host
        if not getattr(host, "scale_memory_status_label", None):
            return
        policy = self.current_scale_memory_policy()
        if not policy.enabled:
            host.scale_memory_status_label.setText(
                "缩放记忆已关闭；本次启动中已有的学习记录会保留，但不会参与搜索。"
            )
            return
        summaries = host.engine.scale_memory_store.summaries(policy, maximum=6)
        if not summaries:
            manual = format_manual_scales(policy.manual_scales)
            suffix = f"；手动优先倍率：[{manual}]" if manual else ""
            host.scale_memory_status_label.setText(
                "本次启动尚未学习到缩放倍率；首次成功识别后会显示当前优先倍率和算法历史上限"
                f"{suffix}。"
            )
            return
        host.scale_memory_status_label.setText(
            "\n".join(summary.status_text() for summary in summaries)
        )

    def clear_scale_memory_session(self):
        host = self.host
        host.engine.scale_memory_store.clear()
        self.refresh_scale_memory_status()
        host.log_setting_change("缩放记忆", "已清除本次自动学习记录")

    # ------------------------------------------------------------------
    # Scan region configuration (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def normalize_region_list(self, regions):
        normalized = []
        for region in regions or []:
            try:
                x, y, w, h = [int(float(v)) for v in region]
                if w > 0 and h > 0:
                    normalized.append((x, y, w, h))
            except Exception as exc:
                logger.debug("解析区域失败: %s", exc, exc_info=True)
                continue
        return normalized

    def update_region_label(self):
        host = self.host
        regions = self.normalize_region_list(getattr(host.engine, "scan_regions", []))
        if regions:
            host.region_label.setText(f"范围: 多区域 {len(regions)} 个")
            return
        if host.engine.scan_region:
            host.region_label.setText(f"范围(物理): {host.engine.scan_region}")
        else:
            host.region_label.setText("范围: 全屏")

    def apply_scan_region_config(self, scan_region=None, scan_regions=None):
        host = self.host
        regions = self.normalize_region_list(scan_regions)
        if regions:
            host.engine.apply_scan_region(scan_regions=regions)
            self.update_region_label()
            return

        single = self.normalize_region_list([scan_region])
        host.engine.apply_scan_region(scan_region=single[0] if single else None)
        self.update_region_label()

    def on_regions_selected(self, rects):
        host = self.host
        regions = self.normalize_region_list(rects)
        if not regions:
            return
        host.engine.apply_scan_region(scan_regions=regions)
        self.update_region_label()
        host.mark_profile_dirty()
        host.append_application_log(f"已锁定 {len(regions)} 个识别区域(物理)，只在这些区域内找图 (速度+++)")
