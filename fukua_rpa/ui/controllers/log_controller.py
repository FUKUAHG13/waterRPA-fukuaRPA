# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Log-panel state, filtering and privacy controls."""

from __future__ import annotations

import time
import weakref
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from ...log_policy import (
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    LOG_APPLICATION,
    LOG_CRITICAL,
    LOG_MODE_CUSTOM,
    LOG_MODE_LABELS,
    LOG_MODE_SIMPLE,
    LogPolicy,
    categories_for_mode,
    normalize_log_categories,
    normalize_log_mode,
)
from ...logging_service import (
    GLOBAL_CONFIG,
    format_local_timestamp,
    publish_log_policy,
    update_log_config,
    write_log,
)
from ...privacy import (
    plain_log_text,
    private_debug_value,
    sensitive_text_metadata,
)
from ...usage_notices import automatic_first_use_notice_pending, usage_notice

if TYPE_CHECKING:
    from .host_view import HostView


# 日志性能首用提醒的独立记录键（与 usage_notices 目录条目一致）。
HEAVY_LOG_WARNING_KEY = usage_notice("logging").one_time_key

SAFE_SETTING_VALUE_NAMES = frozenset(
    {
        "设置视图",
        "界面主题",
        "界面倍率(%)",
        "相似度",
        "最小缩放",
        "最大缩放",
        "缩放步长",
        "二段避让间隔(s)",
        "移动耗时(s)",
        "按住时长(s)",
        "步间隔(s)",
        "单步超时(s)",
        "识别间隔(s)",
        "倍速执行",
        "循环参数",
        "从第X步开始",
        "脚本起始循环",
        "脚本停止循环",
        "灰度匹配",
        "DLL原生识别",
        "原生多核",
        "缩放记忆策略",
        "缩放记忆",
        "学习优先倍率上限",
        "缩放历史记录上限",
        "启用避让",
        "二段避让",
        "避让后操作",
        "任务管理器急停",
        "右上角急停",
        "ESC/中键急停",
        "写入文件日志",
        "显示界面日志",
        "启动时最小化",
        "窗口置顶",
        "运行状态提示",
        "点击位置提示",
        "超时急停",
        "资源监测刷新",
        "自适应降频",
        "画面变化唤醒",
        "画面变化灵敏度",
        "按键映射模式",
        "映射点击方式",
        "启动热键",
        "停止热键",
        "日志级别",
        "日志内容",
        "循环模式",
        "多目标模式",
        "多目标顺序",
        "运行提示位置",
        "显示步骤备注",
        "显示隐藏步骤",
        "点击前激活目标窗口",
        "自定义记忆容量",
        "手动优先倍率",
        "运行前风险提示",
    }
)


class LogController:
    """Own log selection state while treating the main window as a view."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self._control_sync = False
        self._custom_categories = set(DEFAULT_CUSTOM_LOG_CATEGORIES)

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("日志视图已销毁")
        return host

    def append(self, message):
        host = self.host
        started_ns = time.perf_counter_ns()
        try:
            scrollbar = host.log_text.verticalScrollBar()
            is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5
            old_value = scrollbar.value()
            # FR-20260809-D6-⑤: 防御加固——不再固定 redact_paths=False。
            # 常规路径（worker/append_application）调用前已按隐私开关脱敏，二次
            # 脱敏无副作用；若未来有调用方绕过预脱敏直接 append 原始文本，
            # 隐私开启时路径不会上屏（与 write_log 的开关语义对齐）。秘密值
            # 擦除在 plain_log_text 内无条件执行，不受此开关影响。
            protected = bool(GLOBAL_CONFIG.get("privacy_protected", True))
            host.log_text.appendPlainText(
                plain_log_text(message, redact_paths=protected)
            )
            scrollbar.setValue(
                scrollbar.maximum() if is_at_bottom else old_value
            )
        finally:
            host.ui_performance.observe_since("ui.log_append", started_ns)
            host.ui_performance.increment("ui.log_messages")

    def current_mode(self):
        combo = getattr(self.host, "log_level_combo", None)
        value = combo.currentData() if combo is not None else LOG_MODE_SIMPLE
        return normalize_log_mode(value)

    def current_categories(self):
        return normalize_log_categories(self._custom_categories)

    def current_policy(self):
        policy = LogPolicy.create(self.current_mode(), self.current_categories())
        # A5: publish so producers outside the UI apply the same rule. Every
        # reader already funnels through here, so no setter can forget to
        # refresh it and leave the two paths disagreeing.
        publish_log_policy(policy)
        return policy

    def _sync_category_actions(self, categories):
        host = self.host
        selected = set(categories)
        self._control_sync = True
        try:
            for key, action in host.log_category_actions.items():
                action.setChecked(key in selected)
        finally:
            self._control_sync = False
        self.refresh_content_button()

    def refresh_content_button(self):
        host = self.host
        if not getattr(host, "log_content_btn", None):
            return
        policy = self.current_policy()
        labels = policy.enabled_labels()
        host.log_content_btn.setText("内容...")
        host.log_content_btn.setToolTip(
            f"当前为{LOG_MODE_LABELS[policy.mode]}日志，共启用 {len(labels)} 项。\n"
            + ("、".join(labels) if labels else "仅输出错误、急停和严重警告")
        )

    def apply_controls(self, mode, custom_categories=None):
        host = self.host
        normalized_mode = normalize_log_mode(mode)
        self._custom_categories = set(
            normalize_log_categories(
                custom_categories
                if custom_categories is not None
                else self._custom_categories
            )
        )
        self._control_sync = True
        try:
            index = host.log_level_combo.findData(normalized_mode)
            host.log_level_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._control_sync = False
        self._sync_category_actions(
            categories_for_mode(normalized_mode, self.current_categories())
        )

    def on_mode_selected(self, _index=None):
        if self._control_sync:
            return
        self._sync_category_actions(
            categories_for_mode(self.current_mode(), self.current_categories())
        )

    def on_category_toggled(self, _category=None):
        if self._control_sync:
            return
        host = self.host
        self._custom_categories = {
            key
            for key, action in host.log_category_actions.items()
            if action.isChecked()
        }
        if self.current_mode() != LOG_MODE_CUSTOM:
            self._control_sync = True
            try:
                host.log_level_combo.setCurrentIndex(
                    host.log_level_combo.findData(LOG_MODE_CUSTOM)
                )
            finally:
                self._control_sync = False
        self.refresh_content_button()
        self.log_setting_change(
            "日志内容", f"自定义启用 {len(self._custom_categories)} 项"
        )

    def select_preset(self, mode):
        host = self.host
        normalized_mode = normalize_log_mode(mode)
        index = host.log_level_combo.findData(normalized_mode)
        if index >= 0:
            host.log_level_combo.setCurrentIndex(index)
            if self.current_mode() == normalized_mode:
                self.on_mode_selected(index)

    def append_application(
        self,
        message,
        category=LOG_APPLICATION,
        *,
        critical=False,
        private_message=None,
    ):
        policy = self.current_policy()
        if not policy.allows(category, critical=critical, message=message):
            return False
        log_to_ui = bool(GLOBAL_CONFIG.get("log_to_ui", True))
        log_to_file = bool(GLOBAL_CONFIG.get("log_to_file", False))
        if not log_to_ui and not log_to_file:
            return False

        privacy_protected = bool(GLOBAL_CONFIG.get("privacy_protected", True))
        if log_to_ui:
            selected = (
                message
                if privacy_protected or private_message is None
                else private_message
            )
            selected = plain_log_text(selected, redact_paths=privacy_protected)
            if policy.timestamp_enabled:
                selected = f"[{format_local_timestamp()}] {selected}"
            self.append(selected)

        # Application/editor events used to bypass the file logger entirely.
        # Keep UI delivery synchronous, but enqueue a separate file-only record.
        if log_to_file:
            write_log(
                message,
                private_message=private_message,
            )
        return True

    def log_setting_change(self, name, value, *, sensitive=None):
        host = self.host
        if host.is_switching_profile:
            return False
        if sensitive is None:
            str_value = str(value)
            if str_value in ("开启", "关闭", "是", "否") or (
                len(str_value) <= 6 and str_value.replace(".", "", 1).replace("-", "", 1).isdigit()
            ):
                is_sensitive = False
            else:
                is_sensitive = str(name) not in SAFE_SETTING_VALUE_NAMES
        else:
            is_sensitive = bool(sensitive)
        if not is_sensitive:
            safe_value = plain_log_text(
                value, max_chars=256, redact_paths=False
            )
            return self.append_application(
                f"设置已生效：{name} = {safe_value}"
            )
        return self.append_application(
            f"<font color='#FF9800'><b>设置已生效：</b>{name}"
            f"（值已脱敏，{sensitive_text_metadata(value)}）</font>",
            private_message=(
                f"<font color='#FF9800'><b>设置已生效：</b>{name} = "
                f"{private_debug_value(value)}</font>"
            ),
        )

    def bind_setting_logs(self):
        """Bind settings widgets to the audit log (C-1: host stage + dialog stage).

        The host stage covers controls created on the main window (startup path);
        the dialog stage covers dialog-owned controls and runs when the dialog is
        built (`_ensure_settings_dialog`). Relative order within each stage is
        preserved — see the load-bearing note on `bind_setting_logs_dialog`.
        """
        self._bind_host_settings_logs()
        if getattr(self.host, "settings_dialog", None) is not None:
            self.bind_setting_logs_dialog()

    def _bind_host_settings_logs(self):
        """Host-owned controls only (must exist before the settings dialog)."""
        host = self.host
        for widget, name in (
            (host.loop_val_edit, "循环参数"),
            (host.hotkey_start_edit, "启动热键"),
            (host.hotkey_stop_edit, "停止热键"),
        ):
            widget.editingFinished.connect(
                lambda _checked=False, w=widget, n=name: host.log_setting_change(
                    n, w.text()
                )
            )
        for widget, name in (
            (host.mini_chk, "启动时最小化"),
            (host.top_chk, "窗口置顶"),
        ):
            widget.stateChanged.connect(
                lambda state, n=name: host.log_setting_change(
                    n, "开启" if state else "关闭"
                )
            )
        for widget, name in (
            (host.log_level_combo, "日志级别"),
            (host.loop_combo, "循环模式"),
        ):
            widget.currentTextChanged.connect(
                lambda text, n=name: host.log_setting_change(n, text)
            )
        host.ui_scale_edit.editingFinished.connect(host.apply_ui_scale_from_edit)
        # C-1：这三个控件是**宿主态**（`_build_host_owned_settings_controls` 创建），
        # 按归属分组，绑定必须留在宿主阶段，否则首开前改动不会进审计日志。
        for widget, name in (
            (host.log_file_chk, "写入文件日志"),
            (host.log_ui_chk, "显示界面日志"),
        ):
            widget.stateChanged.connect(
                lambda state, n=name: host.log_setting_change(
                    n, "开启" if state else "关闭"
                )
            )
        host.mapping_click_mode_combo.currentTextChanged.connect(
            lambda text: host.log_setting_change("映射点击方式", text)
        )

    def bind_setting_logs_dialog(self):
        """Connect dialog-owned setting widgets to the audit log.

        Moved from the main window so the log controller owns the only slot
        that exists purely to feed the setting-change audit log. The connection
        order below is load-bearing: several widgets have multiple signals wired
        in a specific sequence (e.g. native_core_chk / scale_memory_*), so the
        order must not change when touching this method.
        """
        host = self.host
        text_bindings = [
            (host.conf_edit, "相似度"),
            (host.scale_min, "最小缩放"),
            (host.scale_max, "最大缩放"),
            (host.scale_step, "缩放步长"),
            (host.dodge_x1, "避让坐标1 X"),
            (host.dodge_y1, "避让坐标1 Y"),
            (host.dodge_x2, "避让坐标2 X"),
            (host.dodge_y2, "避让坐标2 Y"),
            (host.dbl_wait, "二段避让间隔(s)"),
            (host.move_spd, "移动耗时(s)"),
            (host.click_hld, "按住时长(s)"),
            (host.move_jitter, "移动随机延迟(s)"),
            (host.click_hld_jitter, "按住随机延迟(s)"),
            (host.settle, "步间隔(s)"),
            (host.timeout, "单步超时(s)"),
            (host.detect_delay, "识别间隔(s)"),
            (host.playback_speed, "倍速执行"),
            (host.start_step_edit, "从第X步开始"),
            (host.loop_start_round_edit, "脚本起始循环"),
            (host.loop_end_round_edit, "脚本停止循环"),
            (host.multi_collect_first_edit, "首次命中缓冲"),
            (host.multi_collect_last_edit, "末位缓冲"),
            (host.multi_collect_max_edit, "最长收集时间"),
            (host.multi_collect_targets_edit, "最多点击目标数"),
            (host.mouse_move_settle, "移动后等待(s)"),
        ]
        for widget, name in text_bindings:
            widget.editingFinished.connect(
                lambda _checked=False, w=widget, n=name: host.log_setting_change(
                    n, w.text()
                )
            )

        check_bindings = [
            (host.gray_chk, "灰度匹配"),
            (host.dodge_chk, "启用避让"),
            (host.double_dodge_chk, "二段避让"),
            (host.tm_failsafe, "任务管理器急停"),
            (host.tr_failsafe, "右上角急停"),
            (host.key_failsafe, "ESC/中键急停"),
            (host.run_status_chk, "运行状态提示"),
            (host.click_indicator_chk, "点击位置提示"),
            (host.mouse_trail_chk, "鼠标移动拖影"),
            (host.timeout_stop_chk, "超时急停"),
            (host.adaptive_backoff_chk, "自适应降频"),
            (host.activate_before_click_chk, "点击前激活目标窗口"),
            (host.mapping_mode_chk, "按键映射模式"),
            (host.multi_collect_partial_chk, "目标不足时点击现有目标并完成"),
            (host.keyboard_scan_code_chk, "键盘使用扫描码"),
        ]
        for widget, name in check_bindings:
            widget.stateChanged.connect(
                lambda state, n=name: host.log_setting_change(
                    n, "开启" if state else "关闭"
                )
            )

        combo_bindings = [
            (host.native_parallel_combo, "原生多核"),
            (host.scale_memory_tier_combo, "缩放记忆策略"),
            (host.dodge_click_combo, "避让后操作"),
            (host.cpu_refresh_combo, "资源监测刷新"),
            (host.scene_wake_sensitivity_combo, "画面变化灵敏度"),
            (host.multi_mode_combo, "多目标模式"),
            (host.multi_order_combo, "多目标顺序"),
            (host.multi_order_b_combo, "第二排序条件"),
            (host.input_backend_combo, "输入方式"),
            (host.run_status_pos_combo, "运行提示位置"),
        ]
        for widget, name in combo_bindings:
            widget.currentTextChanged.connect(
                lambda text, n=name: host.log_setting_change(n, text)
            )

        # Widgets with extra connections or non-standard read expressions keep
        # explicit bindings so their connection order and behavior stay intact.
        # （ui_scale_edit 已由宿主态阶段绑定，C-1）

        # FR-20260812-D12-002: 三项设置此前零 signal 绑定，改动不触发
        # mark_profile_dirty，3s 自动保存不落盘（仅崩溃/强杀场景丢失）。
        host.step_adjust_follow_chk.stateChanged.connect(
            lambda state: host.log_setting_change(
                "调整跟随", "开启" if state else "关闭"
            )
        )
        host.sound_alert_audio_path_edit.editingFinished.connect(
            lambda: host.log_setting_change(
                "声音提示音频", host.sound_alert_audio_path_edit.text()
            )
        )
        host.sound_alert_play_seconds_edit.editingFinished.connect(
            lambda: host.log_setting_change(
                "声音提示播放时长", host.sound_alert_play_seconds_edit.text()
            )
        )

        # FR-20260813-D15-001: 同根因遗漏——下列 5 项全局设置此前未接脏标记信号，
        # 修改后不触发 mark_profile_dirty，3s 自动保存不落盘，关闭窗口静默丢失。
        host.click_spatial_jitter.editingFinished.connect(
            lambda: host.log_setting_change("点击偏移", host.click_spatial_jitter.text())
        )
        host.move_curve_strength.editingFinished.connect(
            lambda: host.log_setting_change("移动曲线", host.move_curve_strength.text())
        )
        host.key_hold_duration.editingFinished.connect(
            lambda: host.log_setting_change("按键按住", host.key_hold_duration.text())
        )
        host.time_limit_stop_mode_combo.currentTextChanged.connect(
            lambda text: host.log_setting_change("限时停止", text)
        )
        host.isolated_runner_chk.stateChanged.connect(
            lambda state: host.log_setting_change("独立Runner", "开启" if state else "关闭")
        )

        host.native_core_chk.stateChanged.connect(
            lambda s: host.log_setting_change("DLL原生识别", "开启" if s else "关闭")
        )
        host.native_core_chk.toggled.connect(host.update_native_optimization_ui)
        host.native_scale_hint_chk.stateChanged.connect(
            lambda state: host.log_setting_change(
                "缩放记忆", "开启" if state else "关闭"
            )
        )
        host.native_scale_hint_chk.toggled.connect(host.refresh_scale_memory_status)
        host.scale_memory_tier_combo.currentIndexChanged.connect(
            host.refresh_scale_memory_status
        )
        host.scale_memory_manual_edit.editingFinished.connect(
            lambda: host.log_setting_change(
                "手动优先倍率", host.scale_memory_manual_edit.text() or "未设置"
            )
        )
        host.scale_memory_manual_edit.editingFinished.connect(
            host.refresh_scale_memory_status
        )
        host.scale_memory_custom_chk.toggled.connect(
            host.update_scale_memory_custom_ui
        )
        host.scale_memory_custom_chk.toggled.connect(
            host.refresh_scale_memory_status
        )
        host.scale_memory_custom_chk.stateChanged.connect(
            lambda state: host.log_setting_change(
                "自定义记忆容量", "开启" if state else "关闭"
            )
        )
        host.scale_memory_preferred_spin.valueChanged.connect(
            lambda value: host.log_setting_change("学习优先倍率上限", value)
        )
        host.scale_memory_preferred_spin.valueChanged.connect(
            host.refresh_scale_memory_status
        )
        host.scale_memory_history_spin.valueChanged.connect(
            lambda value: host.log_setting_change("缩放历史记录上限", value)
        )
        host.scale_memory_history_spin.valueChanged.connect(
            host.refresh_scale_memory_status
        )
        host.scale_memory_clear_btn.clicked.connect(host.clear_scale_memory_session)
        host.log_privacy_chk.toggled.connect(host.on_log_privacy_changed)
        host.log_ui_chk.toggled.connect(lambda _: self.update_global_config())
        host.log_file_chk.toggled.connect(lambda _: self.update_global_config())
        host.risk_tip_chk.toggled.connect(host.on_risk_tip_changed)
        host.first_use_notices_chk.toggled.connect(
            host.on_first_use_notices_changed
        )
        host.scene_wake_chk.stateChanged.connect(
            lambda state: host.log_setting_change(
                "画面变化唤醒", "开启" if state else "关闭"
            )
        )
        host.scene_wake_chk.toggled.connect(
            host.scene_wake_sensitivity_combo.setEnabled
        )
        host.log_level_combo.currentTextChanged.connect(host.warn_heavy_log_level)

    def warn_heavy_level(self, text):
        host = self.host
        if host.is_switching_profile or text == "简易":
            return
        if not automatic_first_use_notice_pending(
            host.settings, HEAVY_LOG_WARNING_KEY
        ):
            return
        QMessageBox.warning(
            host,
            "日志性能提示",
            usage_notice("logging").body,
        )
        host.settings.setValue(HEAVY_LOG_WARNING_KEY, "1")
        host.settings.sync()

    def update_global_config(self):
        host = self.host
        update_log_config(
            log_to_file=host.log_file_chk.isChecked(),
            log_to_ui=host.log_ui_chk.isChecked(),
            privacy_protected=host.log_privacy_chk.isChecked(),
        )

    def refresh_privacy_ui(self):
        host = self.host
        protected = host.log_privacy_chk.isChecked()
        host.log_privacy_status_label.setVisible(not protected)
        host.log_privacy_chk.setToolTip(
            "当前已开启：日志会隐藏普通输入、变量值、窗口条件、完整路径和错误详情。"
            if protected
            else "当前已关闭：详细/完全日志可能包含普通输入、变量值、窗口条件、完整路径和错误详情。"
        )
        host.settings_controller.refresh_advanced_settings_notice()

    def on_privacy_changed(self, protected):
        host = self.host
        protected = bool(protected)
        if not protected:
            choice = QMessageBox.warning(
                host,
                "关闭日志隐私保护",
                "关闭后，详细/完全日志可能记录普通输入文字、变量值、窗口条件、"
                "完整路径和错误详情。请只在自己的设备上用于排错，并在分享日志前重新开启。\n\n"
                "凭据库秘密值和“输入秘密文本”的原文仍然不会记录；诊断包与崩溃报告也不会取消脱敏。",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if choice != QMessageBox.Yes:
                host.log_privacy_chk.blockSignals(True)
                host.log_privacy_chk.setChecked(True)
                host.log_privacy_chk.blockSignals(False)
                protected = True

        update_log_config(privacy_protected=protected)
        host.settings.setValue("log_privacy_protected", protected)
        host.settings.sync()
        self.refresh_privacy_ui()
        if not host.is_switching_profile:
            self.append_application(
                "日志隐私保护已开启。"
                if protected
                else "警告：日志隐私保护已关闭；分享日志前请重新开启。",
                LOG_CRITICAL if not protected else LOG_APPLICATION,
                critical=not protected,
            )

    # ------------------------------------------------------------------
    # Log context menu (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def on_log_context_menu(self, pos):
        host = self.host
        menu = host.log_text.createStandardContextMenu(pos)
        menu.addSeparator()
        clear_action = menu.addAction("清空日志")
        clear_action.triggered.connect(host.log_text.clear)
        menu.exec(host.log_text.mapToGlobal(pos))
