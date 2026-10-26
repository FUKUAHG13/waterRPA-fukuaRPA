# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Workflow import, export and portable package UI flows."""

from __future__ import annotations

import json
import logging
import os
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ...config_schema import migrate_profile_config
from ...config_store import atomic_write_json, load_global_mappings
from ...constants import (
    MAX_PROFILE_NAME_LENGTH,
    MAX_PROFILE_STORE_JSON_BYTES,
    MAX_PROFILES,
    PRODUCT_NAME,
    PROFILE_COLLECTION_PACKAGE_FORMAT,
)
from ...json_safety import load_bounded_json_file
from ...profile_model import normalize_profile_name
from ...profile_package import (
    MissingPackageAssetsError,
    build_profile_collection_payload,
    discard_imported_package,
    export_collection_package,
    export_full_package,
    import_collection_package,
    import_full_package,
    inspect_package_format,
    parse_profile_collection_payload,
)
from ...profile_package import (
    asset_export_name as package_asset_export_name,
)
from ...profile_package import (
    collect_profile_image_paths as package_collect_image_paths,
)
from ...profile_package import (
    rewrite_profile_image_paths as package_rewrite_image_paths,
)
from ...profile_package import (
    safe_extract_full_package as package_safe_extract,
)
from ...profile_structure import validate_profile_config_data
from ...profile_validation import validate_profile_for_ui
from ...usage_notices import usage_notice
from ...workflow_security import (
    analyze_workflow_capabilities,
    disable_dangerous_workflow_actions,
)
from ..components import NoWheelComboBox, fit_combo_to_contents, fit_text_button

if TYPE_CHECKING:
    from .host_view import HostView



logger = logging.getLogger(__name__)
class WorkflowTransferController:
    """Move workflow data between the editor, JSON files and portable ZIPs."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已销毁")
        return host

    @staticmethod
    def asset_export_name(path):
        return package_asset_export_name(path)

    @staticmethod
    def rewrite_profile_image_paths(cfg, mapper):
        return package_rewrite_image_paths(cfg, mapper)

    @staticmethod
    def collect_profile_image_paths(cfg):
        return package_collect_image_paths(cfg)

    def save(self):
        host = self.host
        data = host.get_current_ui_config()
        path, _ = QFileDialog.getSaveFileName(
            host,
            "普通导出",
            f"{host.current_profile_name}_普通方案.json",
            filter="JSON (*.json)",
        )
        if not path:
            return
        # 超限方案不得被写出：`atomic_write_json` 只负责原子落盘，不做任何结构
        # 校验。导出文件无法再导入（保存入口会拒绝），用户却拿到一个"成功导出"
        # 的提示，问题被推迟到下次导入才暴露。导出前先做与保存同一套结构校验。
        error = validate_profile_config_data(data, "方案")
        if error:
            QMessageBox.warning(host, "无法导出", error)
            return
        try:
            atomic_write_json(path, data)
            host.append_application_log(
                "<font color='green'><b>>>> 方案已成功导出到用户指定位置</b></font>"
            )
        except Exception as error:
            QMessageBox.warning(host, "导出失败", str(error))

    def _build_batch_export_dialog(self):
        host = self.host
        dialog = QDialog(host)
        dialog.setWindowTitle("批量导出方案")
        dialog.resize(520, 520)
        layout = QVBoxLayout(dialog)
        tip = QLabel("勾选要导出的方案；导出和再次导入都会保持这里的排列顺序。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        profile_list = QListWidget()
        for name in host.profiles_data:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            profile_list.addItem(item)
        layout.addWidget(profile_list, 1)

        selection_row = QHBoxLayout()
        select_all = fit_text_button(QPushButton("全选"), 64)
        select_none = fit_text_button(QPushButton("全不选"), 64)
        select_all.setObjectName("batchSelectAll")
        select_none.setObjectName("batchSelectNone")
        select_all.clicked.connect(
            lambda: [
                profile_list.item(index).setCheckState(Qt.Checked)
                for index in range(profile_list.count())
            ]
        )
        select_none.clicked.connect(
            lambda: [
                profile_list.item(index).setCheckState(Qt.Unchecked)
                for index in range(profile_list.count())
            ]
        )
        selection_row.addWidget(select_all)
        selection_row.addWidget(select_none)
        selection_row.addStretch()
        layout.addLayout(selection_row)

        export_type_row = QHBoxLayout()
        export_type_row.addWidget(QLabel("导出类型:"))
        export_type = NoWheelComboBox()
        export_type.addItem("普通方案集合（不复制图片）", "json")
        export_type.addItem("完整迁移集合包（包含图片）", "portable")
        export_type.addItem("脱敏分享集合包", "sanitized")
        fit_combo_to_contents(export_type)
        export_type_row.addWidget(export_type)
        export_type_row.addStretch()
        layout.addLayout(export_type_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("继续")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")

        def accept_selection():
            if not any(
                profile_list.item(index).checkState() == Qt.Checked
                for index in range(profile_list.count())
            ):
                QMessageBox.warning(dialog, "无法导出", "请至少选择一个方案。")
                return
            dialog.accept()

        buttons.accepted.connect(accept_selection)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if hasattr(host, "apply_ui_scale_to_widget"):
            host.apply_ui_scale_to_widget(dialog)
        return dialog, profile_list, export_type

    def _choose_batch_export(self):
        dialog, profile_list, export_type = self._build_batch_export_dialog()
        if dialog.exec() != QDialog.Accepted:
            return None
        names = [
            profile_list.item(index).data(Qt.UserRole)
            for index in range(profile_list.count())
            if profile_list.item(index).checkState() == Qt.Checked
        ]
        return names, str(export_type.currentData())

    def save_batch(self):
        host = self.host
        host.profiles_data[host.current_profile_name] = host.get_current_ui_config()
        choice = self._choose_batch_export()
        if choice is None:
            return
        names, export_type = choice
        selected = {name: host.profiles_data[name] for name in names}
        if export_type == "portable" and not self.confirm_portable_export_privacy():
            return
        suffix = {
            "json": "普通方案集合",
            "portable": "完整迁移集合包",
            "sanitized": "脱敏分享集合包",
        }[export_type]
        extension = ".json" if export_type == "json" else ".zip"
        path, _ = QFileDialog.getSaveFileName(
            host,
            "批量导出方案",
            f"{PRODUCT_NAME}_{suffix}{extension}",
            filter=("JSON (*.json)" if extension == ".json" else f"{PRODUCT_NAME} 方案集合包 (*.zip)"),
        )
        if not path:
            return
        if not path.lower().endswith(extension):
            path += extension
        try:
            if export_type == "json":
                payload = build_profile_collection_payload(selected)
                # 与单方案导出同一判据：超限方案不得被写出。批量路径此前只查
                # 集合总字节数，逐方案的步骤数上限完全绕过。
                for name, profile in selected.items():
                    error = validate_profile_config_data(profile, f"方案“{name}”")
                    if error:
                        raise ValueError(error)
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if len(encoded) > MAX_PROFILE_STORE_JSON_BYTES:
                    raise ValueError("方案集合正文大小超过安全上限")
                atomic_write_json(path, payload)
            else:
                result = export_collection_package(
                    selected,
                    path,
                    privacy_protected=export_type == "sanitized",
                    base_dir=host.base_dir,
                )
                if result.privacy_protected:
                    QMessageBox.information(
                        host,
                        "脱敏分享集合包已生成",
                        f"已导出 {result.profile_count} 个方案，并停用了 "
                        f"{result.redacted_step_count} 个包含隐私内容的步骤。",
                    )
            host.append_application_log(
                f"<font color='green'><b>>>> 已按原顺序批量导出 {len(names)} 个方案</b></font>"
            )
        except MissingPackageAssetsError as error:
            preview = "\n".join(error.paths[:8])
            QMessageBox.warning(
                host,
                "批量导出已取消",
                f"有 {len(error.paths)} 个图片路径不存在，本次没有生成残缺集合包。\n\n{preview}",
            )
        except Exception as error:
            QMessageBox.warning(host, "批量导出失败", str(error))

    def save_portable_package(self):
        if not self.confirm_portable_export_privacy():
            return
        self._save_package(
            privacy_protected=False,
            dialog_title="导出完整迁移包",
            default_suffix="完整迁移包",
            filter_label=f"{PRODUCT_NAME} 完整迁移包",
        )

    def save_sanitized_package(self):
        self._save_package(
            privacy_protected=True,
            dialog_title="导出脱敏分享包",
            default_suffix="脱敏分享包",
            filter_label=f"{PRODUCT_NAME} 脱敏分享包",
        )

    def save_full_package(self):
        """Compatibility alias for integrations using the former method name."""

        return self.save_portable_package()

    def _save_package(
        self,
        *,
        privacy_protected,
        dialog_title,
        default_suffix,
        filter_label,
    ):
        host = self.host
        data = host.get_current_ui_config()
        default_name = f"{host.current_profile_name}_{default_suffix}.zip"
        path, _ = QFileDialog.getSaveFileName(
            host,
            dialog_title,
            default_name,
            filter=f"{filter_label} (*.zip)",
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            result = export_full_package(
                data,
                host.current_profile_name,
                path,
                privacy_protected=privacy_protected,
                base_dir=host.base_dir,
            )
            host.append_application_log(
                f"<font color='green'><b>>>> {default_suffix}已成功导出到用户指定位置</b></font>"
            )
            if result.privacy_protected:
                QMessageBox.information(
                    host,
                    "脱敏分享包已生成",
                    f"已隐藏可能包含个人信息的文字、窗口、程序路径和模板图片，"
                    f"并停用了 {result.redacted_step_count} 个受影响步骤。\n\n"
                    "该文件适合发给他人排查或参考流程结构，不是可直接运行的跨电脑迁移包。"
                    "凭据值始终不会写入任何导出文件。",
                )
        except MissingPackageAssetsError as error:
            preview = "\n".join(error.paths[:8])
            if len(error.paths) > 8:
                preview += f"\n……另有 {len(error.paths) - 8} 项"
            QMessageBox.warning(
                host,
                f"{default_suffix}导出已取消",
                f"有 {len(error.paths)} 个图片路径不存在。为避免生成换电脑后无法运行的残缺包，"
                f"本次没有导出。\n\n{preview}",
            )
        except Exception as error:
            QMessageBox.warning(host, f"{default_suffix}导出失败", str(error))

    @staticmethod
    def safe_extract_full_package(zip_path, target_dir):
        return package_safe_extract(zip_path, target_dir)

    def load_full_package(self, path):
        return import_full_package(path, self.host.base_dir)

    def confirm_portable_export_privacy(self):
        host = self.host
        if host.settings.value(
            "portable_export_privacy_notice_seen", False, type=bool
        ):
            return True

        dialog = QMessageBox(host)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle("完整迁移包包含私人内容")
        dialog.setText("完整迁移包用于把可运行方案迁移到自己的其他电脑。")
        dialog.setInformativeText(
            usage_notice("portable_export").body
        )
        continue_button = dialog.addButton("继续导出", QMessageBox.AcceptRole)
        dialog.addButton("取消", QMessageBox.RejectRole)
        dont_show_again = QCheckBox("以后不再提示")
        dialog.setCheckBox(dont_show_again)
        dialog.exec()
        if dialog.clickedButton() is not continue_button:
            return False
        if dont_show_again.isChecked():
            host.settings.setValue("portable_export_privacy_notice_seen", True)
        return True

    def review_imported_workflow(self, profile, *, privacy_protected=False):
        report = analyze_workflow_capabilities(profile)
        dialog = QDialog(self.host)
        dialog.setWindowTitle("导入前检查")
        dialog.resize(620, 500)
        self.host.apply_ui_scale_to_widget(dialog)
        layout = QVBoxLayout(dialog)
        heading = QLabel("该方案不会自动运行。请先确认它将使用哪些电脑能力。")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        if privacy_protected:
            protected_tip = QLabel(
                "此文件是脱敏包，其中部分步骤已被停用并清除了隐私内容。"
            )
            protected_tip.setProperty("role", "muted")
            protected_tip.setWordWrap(True)
            layout.addWidget(protected_tip)
        summary = QPlainTextEdit()
        summary.setReadOnly(True)
        summary.setPlainText("\n".join(report.summary_lines()))
        layout.addWidget(summary)
        warning = QLabel(
            "“禁用危险步骤后导入”会停用点击、键盘输入、启动/关闭程序、"
            "写入控件及所有已启用的按键映射；等待和判断类步骤会保留。"
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "muted")
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        normal_button = buttons.addButton(
            "按原样导入", QDialogButtonBox.AcceptRole
        )
        disable_button = buttons.addButton(
            "禁用危险步骤后导入", QDialogButtonBox.ActionRole
        )
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        choice = {"value": None}

        def accept_normal():
            choice["value"] = "normal"
            dialog.accept()

        def accept_disabled():
            choice["value"] = "disabled"
            dialog.accept()

        normal_button.clicked.connect(accept_normal)
        disable_button.clicked.connect(accept_disabled)
        buttons.rejected.connect(dialog.reject)
        if report.has_warnings:
            disable_button.setDefault(True)
        else:
            normal_button.setDefault(True)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return choice["value"]

    def review_imported_workflows(self, entries, *, privacy_protected=False):
        if len(entries) == 1:
            return self.review_imported_workflow(
                entries[0][1], privacy_protected=privacy_protected
            )
        reports = [
            (name, analyze_workflow_capabilities(profile))
            for name, profile in entries
        ]
        dialog = QDialog(self.host)
        dialog.setWindowTitle("批量导入前检查")
        dialog.resize(700, 560)
        self.host.apply_ui_scale_to_widget(dialog)
        layout = QVBoxLayout(dialog)
        heading = QLabel(
            f"将按文件中的顺序导入 {len(entries)} 个方案。所有方案都不会自动运行。"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)
        if privacy_protected:
            protected_tip = QLabel("此文件是脱敏集合包，部分步骤已被停用。")
            protected_tip.setProperty("role", "muted")
            layout.addWidget(protected_tip)
        summary = QPlainTextEdit()
        summary.setReadOnly(True)
        lines = []
        for name, report in reports:
            lines.append(f"【{name}】")
            lines.extend(report.summary_lines())
            lines.append("")
        summary.setPlainText("\n".join(lines).rstrip())
        layout.addWidget(summary, 1)
        warning = QLabel(
            "“全部禁用危险步骤后导入”会对集合中的每个方案应用同一安全处理。"
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "muted")
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        normal_button = buttons.addButton("全部按原样导入", QDialogButtonBox.AcceptRole)
        disable_button = buttons.addButton(
            "全部禁用危险步骤后导入", QDialogButtonBox.ActionRole
        )
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        choice = {"value": None}

        def accept(value):
            choice["value"] = value
            dialog.accept()

        normal_button.clicked.connect(lambda: accept("normal"))
        disable_button.clicked.connect(lambda: accept("disabled"))
        buttons.rejected.connect(dialog.reject)
        if any(report.has_warnings for _name, report in reports):
            disable_button.setDefault(True)
        else:
            normal_button.setDefault(True)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return choice["value"]

    @staticmethod
    def _available_import_name(base_name, reserved_names):
        base = normalize_profile_name(base_name)
        name = base
        counter = 1
        while name in reserved_names:
            suffix = f"_{counter}"
            name = normalize_profile_name(
                base[: MAX_PROFILE_NAME_LENGTH - len(suffix)] + suffix
            )
            counter += 1
        reserved_names.add(name)
        return name

    def load(self):
        host = self.host
        path, _ = QFileDialog.getOpenFileName(
            host,
            "导入方案",
            filter=(
                f"{PRODUCT_NAME} 方案 (*.json *.zip);;JSON (*.json);;"
                "全量包 (*.zip)"
            ),
        )
        if not path:
            return
        package_dir = ""
        keep_import_assets = False
        try:
            if path.lower().endswith(".zip"):
                package_format = inspect_package_format(path)
                if package_format == PROFILE_COLLECTION_PACKAGE_FORMAT:
                    collection_result = import_collection_package(
                        path, host.base_dir
                    )
                    entries = list(collection_result.profiles)
                    package_dir = collection_result.package_dir
                    privacy_protected = collection_result.privacy_protected
                else:
                    import_result = self.load_full_package(path)
                    entries = [
                        (import_result.suggested_name, import_result.profile)
                    ]
                    package_dir = import_result.package_dir
                    privacy_protected = import_result.privacy_protected
            else:
                data = load_bounded_json_file(
                    path,
                    max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
                    # FR-20261011-006 / DOC-SCOPE005-001: 集合 JSON 的节点上限必须与
                    # 导出侧同一口径。导出侧只校验聚合字节（MAX_PROFILE_STORE_JSON_BYTES）
                    # 与逐方案节点，若此处沿用默认 2M 节点校验整份正文，会出现"导出成功
                    # 但本版本无法导入"的方案集合。JSON 节点数 ≤ 序列化字节数，故以字节
                    # 上限作节点上限，保证"能导出即可导入"（与 config_store/profile_package
                    # 的集合级加载同口径）。
                    max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
                    label="导入方案",
                )
                base_name = os.path.splitext(os.path.basename(path))[0]
                collection_entries = parse_profile_collection_payload(data)
                if collection_entries is not None:
                    entries = collection_entries
                elif isinstance(data, list):
                    new_profile_data = host.get_default_config_dict()
                    new_profile_data["tasks"] = data
                    entries = [(base_name, new_profile_data)]
                elif isinstance(data, dict):
                    entries = [(base_name, migrate_profile_config(data).value)]
                else:
                    raise ValueError("无法识别的配置文件格式")
                privacy_protected = False

            if len(host.profiles_data) + len(entries) > MAX_PROFILES:
                raise ValueError(
                    f"导入后方案数量会超过上限 {MAX_PROFILES}，请减少选择或先删除旧方案"
                )
            reserved_names = set(host.profiles_data)
            prepared_entries = []
            for base_name, profile in entries:
                new_name = self._available_import_name(base_name, reserved_names)
                profile_error = host.profile_controller.validate_profile_config_data(
                    profile, f"方案“{new_name}”"
                )
                if profile_error:
                    raise ValueError(profile_error)
                prepared_entries.append(
                    (new_name, validate_profile_for_ui(profile))
                )

            review_choice = self.review_imported_workflows(
                prepared_entries,
                privacy_protected=privacy_protected,
            )
            if review_choice is None:
                if package_dir:
                    discard_imported_package(package_dir, host.base_dir)
                return
            if review_choice == "disabled":
                prepared_entries = [
                    (name, disable_dangerous_workflow_actions(profile))
                    for name, profile in prepared_entries
                ]

            old_name = host.current_profile_name
            host.profiles_data[old_name] = host.get_current_ui_config()
            old_profiles = host.profile_model.snapshot()
            for new_name, new_profile_data in prepared_entries:
                host.profile_model.create(new_name, new_profile_data)
            first_imported_name = prepared_entries[0][0]
            # 脱敏包会清空 key_mappings；若原样当作当前方案保存，会把本地全局映射
            # 一并写空（交叉复核 C 第 2 项）。导入后统一用本地全局映射覆盖。
            # 只有「包内没有映射」（脱敏包）时才用本地全局映射补齐；完整迁移包
            # 自带用户映射，覆盖会丢掉导入意图。
            shared = load_global_mappings(getattr(host, "settings", None))
            # 只回填映射本身；mapping_mode_enabled 属用户开关，回填会把「停用危险动作」
            # 的选择改回 True（R16 ④）。逐个方案判定，避免混合包被整体覆盖（R16 ③）。
            payload = {
                key: value
                for key, value in (shared or {}).items()
                if key in ("key_mappings", "key_mapping_count")
            }
            if payload:
                for name, _payload in prepared_entries:
                    profile = host.profiles_data.get(name)
                    if needs_shared_mappings(profile):
                        profile.update(payload)

            host.is_switching_profile = True
            try:
                host.profile_combo.clear()
                host.profile_combo.addItems(list(host.profiles_data))
                host.profile_combo.setCurrentText(first_imported_name)
                host.current_profile_name = first_imported_name
                if not host.apply_ui_config(
                    host.profiles_data[first_imported_name]
                ):
                    error_msg = getattr(host, '_last_config_apply_error', None) or "配置应用失败"
                    raise ValueError(error_msg)
            except Exception as exc:
                logger.warning("导入工作流失败: %s", exc, exc_info=True)
                host.profile_model.replace(old_profiles, old_name)
                host.profile_combo.clear()
                host.profile_combo.addItems(list(host.profiles_data))
                host.profile_combo.setCurrentText(old_name)
                host.current_profile_name = old_name
                host.apply_ui_config(host.profiles_data[old_name])
                raise
            finally:
                host.is_switching_profile = False
            # FR-901-001: 导入方案属方案切换，须清撤销栈，防止 Ctrl+Z 用旧方案
            # 步骤覆盖导入的方案并经自动保存落盘。
            host.profile_controller._clear_editor_history()
            keep_import_assets = True
            host.profile_controller.mark_dirty(force=True)

            host.append_application_log(
                f"<font color='green'><b>>>> 已按原顺序导入并创建 {len(prepared_entries)} 个方案</b></font>"
            )
        except Exception as error:
            if package_dir and not keep_import_assets:
                try:
                    discard_imported_package(
                        package_dir, host.base_dir
                    )
                except OSError as cleanup_exc:
                    logger.warning("导入失败后清理临时目录失败: %s", cleanup_exc, exc_info=True)
            QMessageBox.warning(host, "导入失败", str(error))


def needs_shared_mappings(profile) -> bool:
    """Whether an imported profile must be filled with the local shared mappings.

    脱敏包会清空 `key_mappings`；此时若原样保存，会把本地全局映射一并写空。
    完整迁移包自带映射，不得覆盖（R15 审查：此前测试在测试内重写该逻辑）。
    """
    if not isinstance(profile, dict):
        return False
    return not profile.get("key_mappings")
