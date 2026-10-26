# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Profile persistence, selection and profile-toolbar commands."""

from __future__ import annotations

import contextlib
import copy
import glob
import json
import os
import shutil
import time
import weakref
import zipfile
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ...config_store import (
    archive_existing_backup,
    list_profile_backups,
    list_profile_history,
    load_global_mappings,
    load_profiles_state,
    normalize_profile_backup_count,
    persist_profiles,
    profiles_signature,
    prune_profile_backups,
)
from ...config_store import (
    load_profiles_backup as load_profiles_backup_file,
)
from ...config_store import (
    preserve_corrupt_config as preserve_corrupt_config_file,
)
from ...config_store import (
    profiles_backup_payload as build_profiles_backup_payload,
)
from ...config_store import (
    validate_profile_config_data as validate_profile_data,
)
from ...config_store import (
    validate_profiles_payload as validate_profiles_data,
)
from ...constants import DEFAULT_PROFILE_BACKUP_COUNT, MAX_PROFILES
from ...log_policy import (
    LOG_APPLICATION,
)
from ...logging_service import write_log
from ...privacy import private_error_text, safe_error_name
from ...profile_model import normalize_profile_name
from ...workflow_document import merge_workflow_tasks
from ..lifecycle import is_runtime_editing_locked
from ..settings_defaults_actions import build_new_profile_config

if TYPE_CHECKING:
    from .host_view import HostView


class ProfileController:
    """Coordinate the profile model, persistence store and profile toolbar."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self._dirty_revision = 0
        self._saved_revision = 0

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("方案窗口已销毁")
        return host

    def mark_dirty(self, *, force=False):
        host = self._host_ref()
        if host is None:
            return
        if not force and (
            getattr(host, "is_switching_profile", False)
            or getattr(host, "_close_state_saved", False)
        ):
            return
        self._dirty_revision += 1

    def mark_clean(self, revision=None):
        self._saved_revision = (
            self._dirty_revision if revision is None else int(revision)
        )

    def has_unsaved_changes(self):
        return self._dirty_revision != self._saved_revision

    def set_persistence_status(self, text="", tooltip=""):
        label = getattr(self.host, "profile_save_status_label", None)
        if label is None:
            return
        label.setText(str(text or ""))
        label.setToolTip(str(tooltip or ""))
        label.setVisible(bool(text))

    def validate_profile_config_data(self, cfg, label="方案"):
        return validate_profile_data(cfg, label)

    def validate_profiles_payload(self, profiles):
        return validate_profiles_data(profiles)

    def preserve_corrupt_config(self):
        try:
            return preserve_corrupt_config_file(self.host.config_path)
        except Exception as e:
            write_log(
                f"保存损坏配置副本失败: {safe_error_name(e)}",
                private_message=f"保存损坏配置副本失败: {private_error_text(e)}",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            return ""

    def load_profiles_backup(self):
        return load_profiles_backup_file(self.host.profile_backup_path)

    def backup_count(self):
        return normalize_profile_backup_count(
            self.host.settings.value(
                "profile_backup_count", DEFAULT_PROFILE_BACKUP_COUNT
            )
        )

    def refresh_backup_status(self):
        host = self.host
        spin = getattr(host, "profile_backup_count_spin", None)
        status = getattr(host, "profile_backup_status_label", None)
        configured = self.backup_count()
        if spin is not None and not spin.hasFocus():
            spin.blockSignals(True)
            spin.setValue(configured)
            spin.blockSignals(False)
        paths = list_profile_backups(host.profile_backup_path)
        if status is not None:
            if configured == 0:
                summary = "自动备份已关闭；当前没有保留自动恢复副本。"
            else:
                summary = (
                    f"自动保留上限 {configured} 份；当前共有 {len(paths)} 份。"
                )
            status.setText(
                summary
                + "\n备份位置："
                + os.path.dirname(os.path.abspath(host.profile_backup_path))
            )
        return paths

    def apply_backup_count(self):
        host = self.host
        spin = getattr(host, "profile_backup_count_spin", None)
        if spin is None:
            return False
        previous = self.backup_count()
        requested = normalize_profile_backup_count(spin.value())
        if requested == 0 and previous != 0:
            answer = QMessageBox.warning(
                host.settings_dialog,
                "关闭自动备份",
                "设为 0 会关闭自动备份，并立即删除当前恢复副本和全部历史备份。\n\n"
                "主方案仍会正常保存，但配置损坏后将没有自动恢复来源。是否继续？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                spin.setValue(previous)
                return False
        host.settings.setValue("profile_backup_count", requested)
        host.settings.sync()
        try:
            if requested > 0 and not host.persist_profiles_state(force=True):
                raise OSError("当前方案未能写入新的自动备份")
            prune_profile_backups(host.profile_backup_path, requested)
        except Exception as error:
            host.settings.setValue("profile_backup_count", previous)
            host.settings.sync()
            spin.setValue(previous)
            QMessageBox.warning(
                host.settings_dialog,
                "备份设置未生效",
                f"无法应用备份数量：{safe_error_name(error)}",
            )
            self.refresh_backup_status()
            return False
        self.refresh_backup_status()
        host.append_application_log(
            f"自动备份保留数量已从 {previous} 调整为 {requested}。"
        )
        return True

    def clear_backups(self):
        host = self.host
        paths = list_profile_backups(host.profile_backup_path)
        if not paths:
            QMessageBox.information(host.settings_dialog, "清除备份", "当前没有备份文件。")
            return False
        answer = QMessageBox.question(
            host.settings_dialog,
            "清除全部备份",
            f"将删除当前恢复副本和 {len(list_profile_history(host.profile_backup_path))} 份历史备份。\n\n"
            "保留数量设置不会改变；之后方案再次保存时会按该数量重新创建备份。是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return False
        try:
            prune_profile_backups(host.profile_backup_path, 0)
        except Exception as error:
            QMessageBox.warning(
                host.settings_dialog,
                "清除失败",
                f"无法清除备份：{safe_error_name(error)}",
            )
            return False
        self.refresh_backup_status()
        host.append_application_log(f"已清除 {len(paths)} 份方案备份。")
        return True

    def export_backups(self):
        host = self.host
        paths = list_profile_backups(host.profile_backup_path)
        if not paths:
            QMessageBox.information(host.settings_dialog, "导出备份", "当前没有可导出的备份。")
            return False
        target, _selected = QFileDialog.getSaveFileName(
            host.settings_dialog,
            "导出方案备份",
            os.path.join(host.base_dir, "fukuaRPA_方案备份.zip"),
            "ZIP 压缩包 (*.zip)",
        )
        if not target:
            return False
        if not target.lower().endswith(".zip"):
            target += ".zip"
        target = os.path.abspath(target)
        temp_target = f"{target}.tmp.{os.getpid()}"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zipfile.ZipFile(
                temp_target, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                manifest = {
                    "format": "fukuaRPA_profile_backups_export",
                    "backup_count": len(paths),
                    "files": [],
                }
                for index, path in enumerate(paths):
                    arcname = (
                        "profiles_backup.json"
                        if index == 0
                        and os.path.abspath(path)
                        == os.path.abspath(host.profile_backup_path)
                        else "profiles_history/" + os.path.basename(path)
                    )
                    archive.write(path, arcname)
                    manifest["files"].append(arcname)
                archive.writestr(
                    "BACKUP_INFO.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            os.replace(temp_target, target)
        except Exception as error:
            try:
                if os.path.exists(temp_target):
                    os.remove(temp_target)
            except OSError:
                pass
            QMessageBox.warning(
                host.settings_dialog,
                "导出失败",
                f"无法导出备份：{safe_error_name(error)}",
            )
            return False
        host.append_application_log(
            f"已导出 {len(paths)} 份方案备份。",
            private_msg=f"已导出 {len(paths)} 份方案备份到：{target}",
        )
        QMessageBox.information(host.settings_dialog, "导出完成", f"备份已导出到：\n{target}")
        return True

    def restore_backup(self):
        host = self.host
        paths = list_profile_backups(host.profile_backup_path)
        if not paths:
            QMessageBox.information(host.settings_dialog, "恢复备份", "当前没有可恢复的备份。")
            return False
        labels = []
        for index, path in enumerate(paths):
            stamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
            )
            prefix = (
                "当前恢复副本"
                if index == 0
                and os.path.normcase(os.path.abspath(path))
                == os.path.normcase(os.path.abspath(host.profile_backup_path))
                else "历史备份"
            )
            labels.append(f"{prefix} | {stamp} | {os.path.basename(path)}")
        selected, accepted = QInputDialog.getItem(
            host.settings_dialog,
            "恢复方案备份",
            "选择要恢复的副本：",
            labels,
            0,
            False,
        )
        if not accepted:
            return False
        path = paths[labels.index(selected)]
        profiles, current, error = load_profiles_backup_file(path)
        if error or not profiles:
            QMessageBox.warning(
                host.settings_dialog,
                "备份不可用",
                f"无法读取该备份：{error or '没有方案内容'}",
            )
            return False
        validation_error = validate_profiles_data(profiles)
        if validation_error:
            QMessageBox.warning(host.settings_dialog, "备份不可用", validation_error)
            return False
        answer = QMessageBox.question(
            host.settings_dialog,
            "确认恢复",
            f"该副本包含 {len(profiles)} 个方案。恢复后会替换当前全部方案，并立即保存。\n\n"
            f"来源：{path}\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return False
        old_profiles = host.profile_model.snapshot()
        old_current = host.current_profile_name
        try:
            host.is_switching_profile = True
            host.profile_model.replace(profiles, current)
            host.profile_combo.clear()
            host.profile_combo.addItems(list(host.profiles_data))
            host.profile_combo.setCurrentText(host.current_profile_name)
            if not host.apply_ui_config(host.profiles_data[host.current_profile_name]):
                raise ValueError(host._last_config_apply_error or "方案无法应用到界面")
        except Exception as error:
            host.profile_model.replace(old_profiles, old_current)
            host.profile_combo.clear()
            host.profile_combo.addItems(list(host.profiles_data))
            host.profile_combo.setCurrentText(host.current_profile_name)
            host.apply_ui_config(host.profiles_data[host.current_profile_name])
            QMessageBox.warning(
                host.settings_dialog,
                "恢复失败",
                f"界面已回到恢复前状态：{safe_error_name(error)}",
            )
            return False
        finally:
            host.is_switching_profile = False
        self._clear_editor_history()  # FR-901-001: 恢复备份属方案切换，须清撤销栈
        self.mark_dirty(force=True)
        if not host.persist_profiles_state(force=True):
            QMessageBox.warning(
                host.settings_dialog,
                "恢复后保存失败",
                "备份内容已载入当前界面，但尚未成功写入主配置。请不要关闭程序并先导出方案。",
            )
            return False
        self.refresh_backup_status()
        host.append_application_log(
            f"已从备份恢复 {len(profiles)} 个方案。",
            private_msg=f"已从备份 {path} 恢复 {len(profiles)} 个方案。",
        )
        QMessageBox.information(host.settings_dialog, "恢复完成", "方案备份已恢复并保存。")
        return True

    def profiles_backup_payload(self):
        return build_profiles_backup_payload(self.host.profiles_data, self.host.current_profile_name)

    def persist_profiles_state(self, force=False):
        if getattr(self.host, "_profiles_persistence_blocked", False):
            self.set_persistence_status(
                "方案只读",
                "当前方案未通过安全校验，程序不会覆盖原配置或自动备份。",
            )
            if not self.host._profiles_block_warning_logged:
                write_log(
                    "方案自动保存处于只读保护状态：检测到不兼容或无效的方案，"
                    "未覆盖原配置或自动备份。",
                             category=LOG_APPLICATION,
                             critical=True,
                         )
                self.host._profiles_block_warning_logged = True
            self.mark_clean()
            return True
        if self.host._profiles_save_in_progress or self.host.is_switching_profile:
            return True
        if not force and not self.has_unsaved_changes():
            return True
        saving_revision = self._dirty_revision
        self.host._profiles_save_in_progress = True
        try:
            if self.host.current_profile_name and hasattr(self.host, "task_list"):
                self.host.profiles_data[self.host.current_profile_name] = self.host.get_current_ui_config()
            _payload_text, signature = profiles_signature(
                self.host.profiles_data, self.host.current_profile_name
            )
            if not force and signature == self.host._profiles_save_signature:
                self.mark_clean(saving_revision)
                return True

            self.host._profiles_save_signature = persist_profiles(
                self.host.settings,
                self.host.profile_backup_path,
                self.host.profiles_data,
                self.host.current_profile_name,
                backup_count=self.backup_count(),
                persistence_blocked=getattr(
                    self.host, "_profiles_persistence_blocked", False
                ),
            )
            self.mark_clean(saving_revision)
            self.set_persistence_status()
            return True
        except Exception as e:
            write_log(
                f"自动保存配置失败: {safe_error_name(e)}",
                private_message=f"自动保存配置失败: {private_error_text(e)}",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            self.set_persistence_status(
                "方案未保存",
                f"自动保存失败：{safe_error_name(e)}。修改仍保留在当前窗口中。",
            )
            return False
        finally:
            self.host._profiles_save_in_progress = False

    def autosave_profiles_if_changed(self):
        if getattr(self.host, "_close_state_saved", False):
            return
        self.host.persist_profiles_state(force=False)

    def _archive_migration_backup(self, backup_count: int) -> str:
        """Archive the pre-migration backup; return the archived path or ''.

        DESIGN: when backup_count == 1, history retention is 0 so a regular
        archive entry would be pruned immediately. Copy to a sibling snapshot
        instead and prune old snapshots beyond 3.
        """
        try:
            if backup_count == 1:
                if not os.path.isfile(self.host.profile_backup_path):
                    return ""
                stamp = time.strftime("%Y%m%d_%H%M%S")
                snapshot_path = (
                    f"{self.host.profile_backup_path}.pre_migration_{stamp}.bak"
                )
                shutil.copy2(self.host.profile_backup_path, snapshot_path)
                # 清理超出上限的旧迁移快照
                migration_backups = sorted(glob.glob(
                    self.host.profile_backup_path + ".pre_migration_*.bak"
                ))
                while len(migration_backups) > 3:
                    oldest = migration_backups.pop(0)
                    with contextlib.suppress(OSError):
                        os.remove(oldest)
                return snapshot_path
            return archive_existing_backup(
                self.host.profile_backup_path,
                limit=backup_count - 1,
                min_interval=0,
            )
        except Exception as error:
            write_log(
                f"升级方案前保存历史备份失败: {safe_error_name(error)}",
                private_message=(
                    "升级方案前保存历史备份失败: "
                    f"{private_error_text(error)}"
                ),
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            return ""

    def init_profiles(self):
        backup_count = self.backup_count()
        try:
            prune_profile_backups(self.host.profile_backup_path, backup_count)
        except Exception as error:
            write_log(
                f"应用备份保留数量失败: {safe_error_name(error)}",
                private_message=(
                    "应用备份保留数量失败: " + private_error_text(error)
                ),
                         category=LOG_APPLICATION,
                         critical=True,
                     )
        self.refresh_backup_status()
        result = load_profiles_state(
            self.host.settings,
            self.host.config_path,
            self.host.profile_backup_path,
            backup_count=backup_count,
        )
        self.host._profiles_persistence_blocked = bool(result.persistence_blocked)
        previous_message = self.host._config_recovery_message
        migrated = self.host.profile_model.replace(result.profiles, result.current_profile)
        archived_migration_backup = ""
        if (
            backup_count >= 1
            and (migrated or result.migrated)
            and not self.host._profiles_persistence_blocked
        ):
            archived_migration_backup = self._archive_migration_backup(backup_count)
        if result.recovery_message:
            self.host._config_recovery_message = (
                f"{previous_message}\n\n{result.recovery_message}"
                if previous_message
                else result.recovery_message
            )
        elif previous_message:
            self.host._config_recovery_message = previous_message
        if archived_migration_backup:
            migration_message = (
                "方案格式已兼容升级；升级前的自动备份已保留在：\n"
                f"{archived_migration_backup}"
            )
            self.host._config_recovery_message = (
                f"{self.host._config_recovery_message}\n\n{migration_message}"
                if self.host._config_recovery_message
                else migration_message
            )

        self.host.profile_combo.addItems(list(self.host.profiles_data.keys()))
        last_prof = self.host.profile_model.current_name
        if last_prof in self.host.profiles_data:
            self.host.profile_combo.setCurrentText(last_prof)
        else:
            self.host.profile_combo.setCurrentIndex(0)

        self.host.is_switching_profile = False
        self.host.current_profile_name = self.host.profile_combo.currentText()
        if not self.host.apply_ui_config(
            self.host.profiles_data[self.host.current_profile_name]
        ):
            self.host._profiles_persistence_blocked = True
            apply_message = (
                "当前方案没有通过安全校验，程序已保留原配置并进入只读保护。"
                "请从历史备份恢复，或修正方案文件后重新启动。\n\n"
                f"原因：{self.host._last_config_apply_error}"
            )
            self.host._config_recovery_message = (
                f"{self.host._config_recovery_message}\n\n{apply_message}"
                if self.host._config_recovery_message
                else apply_message
            )
            self.set_persistence_status(
                "方案只读",
                "当前方案未通过安全校验，程序不会覆盖原配置或自动备份。",
            )
        try:
            if self.host._config_recovery_message or migrated or result.migrated:
                self.host._profiles_save_signature = ""
                self.mark_dirty(force=True)
                return
            _payload_text, self.host._profiles_save_signature = profiles_signature(
                self.host.profiles_data, self.host.current_profile_name
            )
        except Exception as exc:
            write_log(
                f"方案签名计算失败: {safe_error_name(exc)}",
                private_message=f"方案签名计算失败: {private_error_text(exc)}",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            self.host._profiles_save_signature = ""
        self.mark_clean()

    def _clear_editor_history(self) -> None:
        """FR-901-001: 方案切换成功后清空撤销/重做栈，防止 Ctrl+Z 把旧方案步骤
        覆盖到新方案（与 on_profile_changed 内清栈语义一致）。供 create/delete/
        restore_backup/import(load) 四条绕过 on_profile_changed 的切换路径调用。
        """
        editor = getattr(self.host, "workflow_editor", None)
        if editor is None:
            return
        undo = getattr(editor, "undo_stack", None)
        redo = getattr(editor, "redo_stack", None)
        if undo is not None:
            undo.clear()
        if redo is not None:
            redo.clear()
        # FR-901-001（返修 #2）：同步重置 push_undo_snapshot 的去重基线
        # （workflow_undo.py:83/89），否则旧方案哈希残留会在新方案首次编辑哈希
        # 恰与旧基线相同时抑制压栈。与 05 报告最小修复方向完全对齐。
        if hasattr(editor, "_last_undo_hash"):
            editor._last_undo_hash = None

    def on_profile_changed(self, new_name):
        if self.host.is_switching_profile:
            return
        old_name = self.host.current_profile_name
        self.host.profiles_data[old_name] = self.host.get_current_ui_config()

        self.host.is_switching_profile = True
        try:
            selected = self.host.profile_model.select(new_name)
            # 映射已全局共享：目标方案里可能残留旧映射，切换前用全局值覆盖，
            # 否则界面会闪回旧映射，并在下一次保存时把旧值写回全局。
            shared = load_global_mappings(getattr(self.host, "settings", None))
            if shared and isinstance(selected, dict):
                selected.update(shared)
            if not self.host.apply_ui_config(selected):
                self.host.profile_model.select(old_name)
                self.host.profile_combo.setCurrentText(old_name)
                QMessageBox.warning(
                    self.host,
                    "无法切换方案",
                    "目标方案没有通过安全校验，当前方案和界面均未改变。\n\n"
                    f"原因：{self.host._last_config_apply_error}",
                )
                return
            # FR-UICORE-001: 方案切换成功后清空撤销/重做栈并重置去重基线
            # （_last_undo_hash），防止 Ctrl+Z 将旧方案步骤覆盖到新方案，
            # 与 _clear_editor_history 的其它切换路径保持一致。
            self._clear_editor_history()
        finally:
            self.host.is_switching_profile = False
        self.mark_dirty(force=True)
        self.host.append_application_log(
            "<b><font color='purple'>>>> 已切换配置方案</font></b>",
            private_msg=f"已从方案“{old_name}”切换到“{new_name}”。",
        )
        # 2026-10-19：按键映射工具窗口按方案展示，切换后同步当前方案名。
        refresh = getattr(self.host, "refresh_key_mapping_tool_profile", None)
        if callable(refresh):
            refresh()

    def create_new_profile(self):
        if len(self.host.profiles_data) >= MAX_PROFILES:
            QMessageBox.warning(self.host, "无法新建", f"方案数量已达到上限 {MAX_PROFILES}。")
            return
        suggested_name = self._next_default_profile_name()
        text, ok = QInputDialog.getText(
            self.host,
            "新建方案",
            "请输入新方案名称:",
            QLineEdit.Normal,
            suggested_name,
        )
        if not ok:
            return
        try:
            text = normalize_profile_name(text)
            if text in self.host.profiles_data:
                raise ValueError("方案名称已存在")
            self.host.profiles_data[self.host.current_profile_name] = (
                self.host.get_current_ui_config()
            )
            # 2026-10-21：新建方案沿用用户保存的「默认设置」模板（未设置则出厂默认）。
            created = self.host.profile_model.create(
                text, config=build_new_profile_config(self.host)
            )
        except ValueError as error:
            QMessageBox.warning(self.host, "无法新建方案", str(error))
            return
        self.host.is_switching_profile = True
        try:
            self.host.profile_combo.addItem(text)
            self.host.profile_combo.setCurrentText(text)
            self.host.apply_ui_config(created)
        finally:
            self.host.is_switching_profile = False
        self._clear_editor_history()  # FR-901-001: 新建方案属方案切换，须清撤销栈
        self.host.append_application_log(
            "已新建方案。",
            private_msg=f"已新建方案“{text}”。",
        )
        self.mark_dirty(force=True)
        self.host.profile_hotkey_controller.on_profiles_changed()

    def duplicate_current_profile(self):
        """Duplicate the current profile (settings + steps) under a new name."""
        # INVARIANT: 复制会切换当前方案并把界面配置写回方案，等同方案切换；
        # 运行期必须与 merge_profile 一样拒绝（ARCHITECTURE #62 / FR-20261021-004）。
        if is_runtime_editing_locked(self.host):
            return False
        if len(self.host.profiles_data) >= MAX_PROFILES:
            QMessageBox.warning(self.host, "无法复制", f"方案数量已达到上限 {MAX_PROFILES}。")
            return
        source_name = self.host.current_profile_name
        suggested = f"{source_name} 副本"
        text, ok = QInputDialog.getText(
            self.host,
            "复制方案",
            "请输入新方案名称:",
            QLineEdit.Normal,
            suggested,
        )
        if not ok:
            return
        try:
            text = normalize_profile_name(text)
            if text in self.host.profiles_data:
                raise ValueError("方案名称已存在")
            # 先把当前界面状态落到源方案，保证复制的是「现在看到的」内容。
            self.host.profiles_data[source_name] = self.host.get_current_ui_config()
            snapshot = copy.deepcopy(self.host.profiles_data[source_name])
            created = self.host.profile_model.create(text, config=snapshot)
        except ValueError as error:
            QMessageBox.warning(self.host, "无法复制方案", str(error))
            return
        self.host.is_switching_profile = True
        try:
            self.host.profile_combo.addItem(text)
            self.host.profile_combo.setCurrentText(text)
            self.host.apply_ui_config(created)
        finally:
            self.host.is_switching_profile = False
        self._clear_editor_history()
        self.host.append_application_log(
            "已复制方案。",
            private_msg=f"已从“{source_name}”复制出“{text}”。",
        )
        self.mark_dirty(force=True)
        self.host.profile_hotkey_controller.on_profiles_changed()

    def _next_default_profile_name(self) -> str:
        """Return the first unused ``新建方案N`` name for the dialog default."""

        existing = {str(name) for name in self.host.profiles_data}
        index = 1
        while f"新建方案{index}" in existing:
            index += 1
        return f"新建方案{index}"

    def rename_current_profile(self):
        old_name = self.host.current_profile_name
        text, ok = QInputDialog.getText(self.host, "重命名方案", "请输入新的方案名称:", QLineEdit.Normal, old_name)
        if not ok:
            return
        try:
            text = normalize_profile_name(text)
            if text == old_name:
                return
            if text in self.host.profiles_data:
                raise ValueError("方案名称已存在")
            self.host.profiles_data[old_name] = self.host.get_current_ui_config()
            self.host.profile_model.rename(text)
        except ValueError as error:
            QMessageBox.warning(self.host, "无法重命名方案", str(error))
            return
        self.host.is_switching_profile = True
        try:
            idx = self.host.profile_combo.findText(old_name)
            if idx >= 0:
                self.host.profile_combo.setItemText(idx, text)
            self.host.profile_combo.setCurrentText(text)
        finally:
            self.host.is_switching_profile = False
        self.host.append_application_log(
            "<font color='#FF9800'><b>>>> 方案已重命名</b></font>",
            private_msg=f"方案已从“{old_name}”重命名为“{text}”。",
        )
        self.mark_dirty(force=True)
        self.host.profile_hotkey_controller.on_profiles_changed()

    def delete_current_profile(self):
        if len(self.host.profiles_data) <= 1:
            QMessageBox.warning(self.host, "错误", "至少需要保留一个方案！")
            return
        del_name = self.host.current_profile_name
        answer = QMessageBox.question(
            self.host,
            "删除方案",
            f"确定删除方案“{del_name}”吗？\n此操作不会删除模板图片。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        del_profile_id = self.host.profile_model.profile_id_of(del_name)
        self.host.profile_model.delete_current()
        # Design §4.3: orphaned scheduled tasks flip to "profile missing" and
        # are disabled so they never run against a vanished profile.
        service = getattr(self.host, "scheduled_service", None)
        if service is not None and del_profile_id:
            service.on_profile_deleted(del_profile_id)

        self.host.is_switching_profile = True
        apply_ok = False
        try:
            idx = self.host.profile_combo.findText(del_name)
            if idx >= 0:
                self.host.profile_combo.removeItem(idx)

            self.host.profile_combo.setCurrentText(self.host.current_profile_name)

            apply_ok = self.host.apply_ui_config(
                self.host.profiles_data[self.host.current_profile_name]
            )
        finally:
            self.host.is_switching_profile = False

        # SAFETY: apply 失败时界面内容仍是"被删方案"，而模型已切到存活方案名。
        # 此时标脏会让 3 秒自动保存（persist_profiles_state 把当前 UI 写入
        # profiles_data[current_profile_name]）把被删方案的内容覆盖到存活方案上，
        # 用户无警告、只能靠历史备份恢复（ARCHITECTURE #63，审查 FR-004）。
        # 方案已从模型中删除，无法回滚选择，因此不标脏、保持界面与磁盘现状，
        # 由用户决定下一步操作。
        if not apply_ok:
            QMessageBox.warning(
                self.host,
                "删除方案后未能刷新界面",
                "方案已从列表中删除，但切换到剩余方案时界面刷新失败，"
                "本次删除尚未写入磁盘。\n\n"
                f"原因：{getattr(self.host, '_last_config_apply_error', '')}\n\n"
                "请勿继续编辑当前界面内容，以免覆盖剩余方案；"
                "可重启程序后确认方案状态。",
            )
            self.host.profile_hotkey_controller.on_profiles_changed()
            return

        self._clear_editor_history()  # FR-901-001: 删除方案属方案切换，须清撤销栈
        self.host.append_application_log(
            "已删除一个方案。",
            private_msg=f"已删除方案“{del_name}”。",
        )
        self.mark_dirty(force=True)
        self.host.profile_hotkey_controller.on_profiles_changed()

    def merge_profile(self):
        host = self.host
        if is_runtime_editing_locked(host):
            return False
        source_names = [
            name for name in host.profiles_data if name != host.current_profile_name
        ]
        if not source_names:
            QMessageBox.information(
                host,
                "无法合并方案",
                "请先创建或导入另一个方案，再将其步骤合并到当前方案。",
            )
            return False

        dialog = QDialog(host)
        dialog.setWindowTitle("合并方案")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        heading = QLabel(
            f"把另一个方案的全部步骤追加到当前方案“{host.current_profile_name}”末尾。"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)
        source_combo = QComboBox()
        source_combo.addItems(source_names)
        source_combo.setToolTip("只合并步骤；另一个方案的全局设置不会覆盖当前方案。")
        layout.addWidget(QLabel("来源方案："))
        layout.addWidget(source_combo)
        preserve_check = QCheckBox("保持来源方案内部的绝对跳转关系（推荐）")
        preserve_check.setChecked(True)
        preserve_check.setToolTip(
            "开启后会用稳定步骤 ID 重建成功、失败及“直到条件成立”的内部跳转。"
            "关闭后会清空来源步骤中的绝对“跳至第几步”，相对“跳过几步”保持不变。"
        )
        layout.addWidget(preserve_check)
        note = QLabel(
            "合并不会修改来源方案，也不会合并全局识别、速度、热键或映射设置。"
            "整个操作只产生一条撤销记录。"
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("合并")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return False

        source_name = source_combo.currentText()
        source_config = host.profiles_data.get(source_name, {})
        source_tasks = source_config.get("tasks", [])
        if not isinstance(source_tasks, list) or not source_tasks:
            QMessageBox.information(host, "无法合并方案", "来源方案没有可合并的步骤。")
            return False

        destination_tasks = host.snapshot_tasks()
        try:
            result = merge_workflow_tasks(
                destination_tasks,
                source_tasks,
                preserve_internal_references=preserve_check.isChecked(),
            )
            candidate = copy.deepcopy(host.get_current_ui_config())
            candidate["tasks"] = result.tasks
            validation_error = validate_profile_data(
                candidate, f"合并后的方案“{host.current_profile_name}”"
            )
            if validation_error:
                raise ValueError(validation_error)
        except Exception as error:
            QMessageBox.warning(
                host,
                "合并失败",
                f"当前方案未发生变化：{safe_error_name(error)}",
            )
            return False

        host.push_undo_state()
        host.restore_task_snapshot(result.tasks)
        self.mark_dirty(force=True)
        host.append_application_log(
            f"已将另一个方案的 {result.appended_count} 个步骤合并到当前方案。",
            private_msg=(
                f"已将方案“{source_name}”的 {result.appended_count} 个步骤合并到当前方案。"
            ),
        )
        reference_text = (
            f"已重建 {result.remapped_references} 处内部绝对跳转。"
            if preserve_check.isChecked()
            else f"已清除 {result.cleared_references} 处来源绝对跳转。"
        )
        QMessageBox.information(
            host,
            "合并完成",
            f"已追加 {result.appended_count} 个步骤。\n{reference_text}\n\n"
            "按 Ctrl+Z 可一次撤销本次合并。",
        )
        return True

    def reorder_profiles(self, names):
        self.host.profiles_data[self.host.current_profile_name] = (
            self.host.get_current_ui_config()
        )
        try:
            self.host.profile_model.reorder(names)
        except ValueError as error:
            QMessageBox.warning(self.host, "无法调整方案顺序", str(error))
            self.host.is_switching_profile = True
            try:
                current_name = self.host.current_profile_name
                self.host.profile_combo.clear()
                self.host.profile_combo.addItems(list(self.host.profiles_data))
                self.host.profile_combo.setCurrentText(current_name)
            finally:
                self.host.is_switching_profile = False
            return False
        self.mark_dirty(force=True)
        self.host.append_application_log(
            f"已调整 {len(names)} 个方案的排列顺序。",
            private_msg="方案新顺序：" + "、".join(str(name) for name in names),
        )
        return True

    def move_profile_up(self):
        idx = self.host.profile_combo.currentIndex()
        if idx > 0:
            target = self.host.profile_model.move_current(-1)
            keys = list(self.host.profiles_data.keys())
            self.host.is_switching_profile = True
            try:
                self.host.profile_combo.clear()
                self.host.profile_combo.addItems(keys)
                self.host.profile_combo.setCurrentIndex(target)
            finally:
                self.host.is_switching_profile = False
            self.mark_dirty(force=True)
            self.host.append_application_log("当前方案已向上移动一位。")

    def move_profile_down(self):
        idx = self.host.profile_combo.currentIndex()
        keys = list(self.host.profiles_data.keys())
        if idx < len(keys) - 1:
            target = self.host.profile_model.move_current(1)
            keys = list(self.host.profiles_data.keys())
            self.host.is_switching_profile = True
            try:
                self.host.profile_combo.clear()
                self.host.profile_combo.addItems(keys)
                self.host.profile_combo.setCurrentIndex(target)
            finally:
                self.host.is_switching_profile = False
            self.mark_dirty(force=True)
            self.host.append_application_log("当前方案已向下移动一位。")
