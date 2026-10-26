# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Read and write global profile settings through the Qt control registry."""

import contextlib
import logging

from ..config_schema import default_profile_config, migrate_profile_config
from ..log_policy import (
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    LOG_APPLICATION,
    normalize_log_mode,
)
from ..logging_service import write_log
from ..privacy import private_error_text, safe_error_name
from ..profile_validation import validate_profile_for_ui
from ..task_model import config_bool
from ..win32_api import parse_hotkey_text

logger = logging.getLogger(__name__)

def capture_global_settings(host):
    """Capture current settings (C-1: host stage always; dialog stage only if built).

    When the settings dialog has not been built yet, dialog-owned fields are taken
    from the existing profile config (base dict) instead of widget reads — the
    dialog cannot have edited them, so keeping the base value is lossless and
    prevents writing empty mappings back to disk.
    """
    if getattr(host, "settings_dialog", None) is None:
        return _capture_global_settings_host_only(host)
    return _capture_global_settings_all(host)


def _capture_global_settings_host_only(host):
    tasks = host.snapshot_tasks()
    log_policy = host.current_log_policy()
    config = dict(host.profiles_data.get(host.current_profile_name, {}))
    config.update(
        {
            "_schema_version": default_profile_config()["_schema_version"],
            "hotkey_start": host.hotkey_start_edit.text().strip(),
            "hotkey_stop": host.hotkey_stop_edit.text().strip(),
            "log_level": log_policy.generation_level,
            "log_mode": log_policy.mode,
            "log_custom_categories": list(host.current_custom_log_categories()),
            "mini": host.mini_chk.isChecked(),
            "top": host.top_chk.isChecked(),
            "ui_scale": host.ui_scale_edit.text(),
            "theme_mode": host.theme_mode_combo.currentData() or "light",
            "loop_mode": host.loop_combo.currentText(),
            "loop_val": host.loop_val_edit.text(),
            "scan_region": host.engine.scan_region,
            "scan_regions": host.engine.scan_regions,
            "key_mapping_count": len(getattr(host, "key_mapping_rows", [])),
            "key_mappings": host.get_key_mappings_config(),
            "tasks": tasks,
        }
    )
    config.pop("low_power_ui", None)
    return migrate_profile_config(config).value


def _capture_global_settings_all(host):
    tasks = host.snapshot_tasks()

    log_policy = host.current_log_policy()
    config = dict(host.profiles_data.get(host.current_profile_name, {}))
    config.update(
        {
            "_schema_version": default_profile_config()["_schema_version"],
            "conf": host.conf_edit.text(),
            "scale_min": host.scale_min.text(),
            "scale_max": host.scale_max.text(),
            "scale_step": host.scale_step.text(),
            "gray_en": host.gray_chk.isChecked(),
            "native_core_en": host.native_core_chk.isChecked(),
            "native_parallel_mode": host.native_parallel_combo.currentData() or "auto",
            "native_scale_hint_en": host.native_scale_hint_chk.isChecked(),
            "isolated_runner_en": host.isolated_runner_chk.isChecked(),
            "scale_memory_tier": host.scale_memory_tier_combo.currentData()
            or "balanced",
            "scale_memory_manual": host.scale_memory_manual_edit.text(),
            "scale_memory_custom_en": host.scale_memory_custom_chk.isChecked(),
            "scale_memory_preferred_limit": host.scale_memory_preferred_spin.value(),
            "scale_memory_history_limit": host.scale_memory_history_spin.value(),
            "dodge_x1": host.dodge_x1.text(),
            "dodge_y1": host.dodge_y1.text(),
            "dodge_x2": host.dodge_x2.text(),
            "dodge_y2": host.dodge_y2.text(),
            "dodge_en": host.dodge_chk.isChecked(),
            "dbl_dodge": host.double_dodge_chk.isChecked(),
            "dbl_wait": host.dbl_wait.text(),
            "dodge_click_action": host.dodge_click_combo.currentData() or "none",
            "move_spd": host.move_spd.text(),
            "click_hld": host.click_hld.text(),
            "move_jitter": host.move_jitter.text(),
            "click_hld_jitter": host.click_hld_jitter.text(),
            "click_spatial_jitter": host.click_spatial_jitter.text(),
            "move_curve_strength": host.move_curve_strength.text(),
            "key_hold_duration": host.key_hold_duration.text(),
            "settle": host.settle.text(),
            "timeout": host.timeout.text(),
            "timeout_stop": host.timeout_stop_chk.isChecked(),
            "detect_delay": host.detect_delay.text(),
            "adaptive_backoff": host.adaptive_backoff_chk.isChecked(),
            "activate_before_click": host.activate_before_click_chk.isChecked(),
            "scene_wake_en": host.scene_wake_chk.isChecked(),
            "scene_wake_sensitivity": host.scene_wake_sensitivity_combo.currentData()
            or "balanced",
            "playback_speed": host.playback_speed.text(),
            "multi_target_mode": host.multi_mode_combo.currentText(),
            "multi_target_order": host.multi_order_combo.currentText(),
            "multi_target_order_b": host.multi_order_b_combo.currentText(),
            "multi_collect_first_hit_s": host.multi_collect_first_edit.text(),
            "multi_collect_last_hit_s": host.multi_collect_last_edit.text(),
            "multi_collect_max_s": host.multi_collect_max_edit.text(),
            "multi_collect_max_targets": host.multi_collect_targets_edit.text(),
            "multi_collect_allow_partial": host.multi_collect_partial_chk.isChecked(),
            "input_backend_mode": host.input_backend_combo.currentText(),
            "keyboard_scan_code": host.keyboard_scan_code_chk.isChecked(),
            "mouse_move_settle_s": host.mouse_move_settle.text(),
            "hotkey_start": host.hotkey_start_edit.text().strip(),
            "hotkey_stop": host.hotkey_stop_edit.text().strip(),
            "log_level": log_policy.generation_level,
            "log_mode": log_policy.mode,
            "log_custom_categories": list(host.current_custom_log_categories()),
            "tm_fs": host.tm_failsafe.isChecked(),
            "tr_fs": host.tr_failsafe.isChecked(),
            "key_fs": host.key_failsafe.isChecked(),
            "log_f": host.log_file_chk.isChecked(),
            "log_ui": host.log_ui_chk.isChecked(),
            "mini": host.mini_chk.isChecked(),
            "top": host.top_chk.isChecked(),
            "run_status_tip": host.run_status_chk.isChecked(),
            "run_status_pos": host.run_status_pos_combo.currentText(),
            "click_indicator": host.click_indicator_chk.isChecked(),
            "mouse_trail": host.mouse_trail_chk.isChecked(),
            "start_step": host.start_step_edit.text(),
            "loop_start_round": host.loop_start_round_edit.text(),
            "loop_end_round": host.loop_end_round_edit.text(),
            "cpu_refresh_interval": host.cpu_refresh_combo.currentData(),
            "ui_scale": host.ui_scale_edit.text(),
            "theme_mode": host.theme_mode_combo.currentData() or "light",
            "loop_mode": host.loop_combo.currentText(),
            "loop_val": host.loop_val_edit.text(),
            "time_limit_stop_mode": host.time_limit_stop_mode_combo.currentData()
            or "finish_loop",
            "scan_region": host.engine.scan_region,
            "scan_regions": host.engine.scan_regions,
            "mapping_mode_enabled": host.mapping_mode_chk.isChecked(),
            "mapping_click_mode": host.mapping_click_mode_combo.currentText(),
            "key_mapping_count": len(getattr(host, "key_mapping_rows", [])),
            "key_mappings": host.get_key_mappings_config(),
            "step_adjust_follow": host.step_adjust_follow_chk.isChecked(),
            "sound_alert_audio_path": host.sound_alert_audio_path_edit.text().strip(),
            "sound_alert_play_seconds": host.sound_alert_play_seconds_edit.text().strip() or "30",
            "tasks": tasks,
        }
    )
    config.pop("low_power_ui", None)
    return migrate_profile_config(config).value


def _int_field(value: object, default: int) -> int:
    """FR-20260812-D12-a: 字段级整数容错。

    接受 int / 整数字符串 / float 可解析且为整数的字符串（"3.0"、"3e0"）；
    其余（含非有限浮点、非数字）回退默认值，避免单字段异常使整份配置
    应用整体回滚。
    """
    try:
        raw = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if raw != raw or raw in (float("inf"), float("-inf")):  # NaN / inf
        return default
    if not raw.is_integer():
        return default
    try:
        return int(raw)
    except (OverflowError, ValueError):
        return default


def _apply_global_settings_unchecked(host, cfg, *, apply_tasks=True):
    """Apply settings (C-1: host stage always; dialog stage only if built).

    ``apply_tasks=False`` is the "fill settings controls only" mode used by the
    deferred settings dialog's first open (FR-20261022-201): the step list and
    any open step-settings dialog must not be touched by opening Settings.
    """
    if getattr(host, "settings_dialog", None) is None:
        _apply_global_settings_host_only(host, cfg, apply_tasks=apply_tasks)
        return
    _apply_global_settings_all(host, cfg, apply_tasks=apply_tasks)


def _apply_global_settings_host_only(host, cfg, *, apply_tasks=True):
    """Apply the startup-relevant subset; dialog-owned widgets are not touched.

    Order mirrors the full applier for the host-owned statements.
    """
    start_parsed = parse_hotkey_text(cfg.get("hotkey_start", "F9"))
    stop_parsed = parse_hotkey_text(cfg.get("hotkey_stop", "F10"))
    host.hotkey_start_edit.setText(
        start_parsed["display"]
        if start_parsed
        else str(cfg.get("hotkey_start", "F9"))
    )
    host.hotkey_stop_edit.setText(
        stop_parsed["display"]
        if stop_parsed
        else str(cfg.get("hotkey_stop", "F10"))
    )
    host.log_controller.apply_controls(
        normalize_log_mode(cfg.get("log_mode"), cfg.get("log_level", 0)),
        cfg.get("log_custom_categories", DEFAULT_CUSTOM_LOG_CATEGORIES),
    )
    host.log_file_chk.setChecked(config_bool(cfg.get("log_f", False)))
    host.log_ui_chk.setChecked(config_bool(cfg.get("log_ui", True)))
    host.mini_chk.setChecked(config_bool(cfg.get("mini", False)))
    host.top_chk.setChecked(config_bool(cfg.get("top", False)))
    host.ui_scale_edit.setText(str(cfg.get("ui_scale", "100")))
    theme_mode = str(cfg.get("theme_mode", "light"))
    theme_index = host.theme_mode_combo.findData(theme_mode)
    host.theme_mode_combo.setCurrentIndex(theme_index if theme_index >= 0 else 0)
    host.window_performance_controller.apply_resource_monitor_interval()
    host.loop_combo.setCurrentText(str(cfg.get("loop_mode", "单次")))
    host.loop_val_edit.setText(str(cfg.get("loop_val", "10")))
    host.apply_scan_region_config(cfg.get("scan_region"), cfg.get("scan_regions"))
    host.mapping_mode_chk.setChecked(
        config_bool(cfg.get("mapping_mode_enabled", False))
    )
    host.mapping_click_mode_combo.setCurrentText(
        str(cfg.get("mapping_click_mode", "真实鼠标点击"))
    )
    host.apply_key_mappings_config(
        cfg.get("key_mappings", []), cfg.get("key_mapping_count", None)
    )
    if apply_tasks:
        host.close_all_task_config_dialogs()
        tasks = cfg.get("tasks", [])
        new_rows = list(tasks)
        host.task_list.clear()
        for d in new_rows:
            host.add_row(
                d,
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        host.update_indexes()
        host.task_list.schedule_virtual_refresh(immediate=True)
    host.log_controller.update_global_config()
    host.log_controller.refresh_privacy_ui()
    host.update_hotkeys()
    host.apply_ui_scale(host.parse_ui_scale_percent() / 100.0)


def _apply_global_settings_all(host, cfg, *, apply_tasks=True):
    cfg = migrate_profile_config(cfg).value
    host.conf_edit.setText(str(cfg.get("conf", "0.8")))
    host.scale_min.setText(str(cfg.get("scale_min", "0.8")))
    host.scale_max.setText(str(cfg.get("scale_max", "1.2")))
    host.scale_step.setText(str(cfg.get("scale_step", "0.05")))
    host.gray_chk.setChecked(config_bool(cfg.get("gray_en", True)))
    host.native_core_chk.setChecked(config_bool(cfg.get("native_core_en", True)))
    native_parallel_mode = str(cfg.get("native_parallel_mode", "auto"))
    native_parallel_index = host.native_parallel_combo.findData(
        native_parallel_mode
    )
    host.native_parallel_combo.setCurrentIndex(
        native_parallel_index if native_parallel_index >= 0 else 0
    )
    host.native_scale_hint_chk.setChecked(
        config_bool(cfg.get("native_scale_hint_en", True))
    )
    host.isolated_runner_chk.setChecked(
        config_bool(cfg.get("isolated_runner_en", False))
    )
    scale_memory_tier = str(cfg.get("scale_memory_tier", "balanced"))
    scale_memory_tier_index = host.scale_memory_tier_combo.findData(
        scale_memory_tier
    )
    host.scale_memory_tier_combo.setCurrentIndex(
        scale_memory_tier_index if scale_memory_tier_index >= 0 else 1
    )
    host.scale_memory_manual_edit.setText(str(cfg.get("scale_memory_manual", "")))
    host.scale_memory_custom_chk.setChecked(
        config_bool(cfg.get("scale_memory_custom_en", False))
    )
    # FR-20260812-D12-a: spin 值经 float 预检链后仍可能收到 "3.0"/"3e0" 等
    # float 可解析但 int() 不可解析的字符串（手工编辑/外部导入）。字段级容错，
    # 避免单字段异常使整份配置应用回滚为"应用配置失败"。
    host.scale_memory_preferred_spin.setValue(
        _int_field(cfg.get("scale_memory_preferred_limit"), 3)
    )
    host.scale_memory_history_spin.setValue(
        _int_field(cfg.get("scale_memory_history_limit"), 64)
    )
    host.update_scale_memory_custom_ui()
    host.update_native_optimization_ui()
    host.dodge_x1.setText(str(cfg.get("dodge_x1", "100")))
    host.dodge_y1.setText(str(cfg.get("dodge_y1", "100")))
    host.dodge_x2.setText(str(cfg.get("dodge_x2", "200")))
    host.dodge_y2.setText(str(cfg.get("dodge_y2", "100")))
    host.dodge_chk.setChecked(config_bool(cfg.get("dodge_en", False)))
    host.double_dodge_chk.setChecked(config_bool(cfg.get("dbl_dodge", False)))
    host.dbl_wait.setText(str(cfg.get("dbl_wait", "0.015")))
    dodge_click_action = str(cfg.get("dodge_click_action", "none"))
    dodge_click_index = host.dodge_click_combo.findData(dodge_click_action)
    host.dodge_click_combo.setCurrentIndex(
        dodge_click_index if dodge_click_index >= 0 else 0
    )

    host.move_spd.setText(str(cfg.get("move_spd", "0.0")))
    host.click_hld.setText(str(cfg.get("click_hld", "0.04")))
    host.move_jitter.setText(str(cfg.get("move_jitter", "0.0")))
    host.click_hld_jitter.setText(str(cfg.get("click_hld_jitter", "0.0")))
    host.click_spatial_jitter.setText(str(cfg.get("click_spatial_jitter", "0")))
    host.move_curve_strength.setText(str(cfg.get("move_curve_strength", "0")))
    host.key_hold_duration.setText(str(cfg.get("key_hold_duration", "0")))
    host.settle.setText(str(cfg.get("settle", "0.5")))
    host.timeout.setText(str(cfg.get("timeout", "0.0")))
    host.timeout_stop_chk.setChecked(config_bool(cfg.get("timeout_stop", False)))
    host.detect_delay.setText(str(cfg.get("detect_delay", "0.1")))
    host.adaptive_backoff_chk.setChecked(
        config_bool(cfg.get("adaptive_backoff", True))
    )
    host.activate_before_click_chk.setChecked(
        config_bool(cfg.get("activate_before_click", False))
    )
    host.scene_wake_chk.setChecked(config_bool(cfg.get("scene_wake_en", True)))
    scene_wake_sensitivity = str(cfg.get("scene_wake_sensitivity", "balanced"))
    scene_wake_index = host.scene_wake_sensitivity_combo.findData(
        scene_wake_sensitivity
    )
    host.scene_wake_sensitivity_combo.setCurrentIndex(
        scene_wake_index if scene_wake_index >= 0 else 1
    )
    host.scene_wake_sensitivity_combo.setEnabled(host.scene_wake_chk.isChecked())
    host.playback_speed.setText(str(cfg.get("playback_speed", "1.0")))
    multi_target_mode = str(cfg.get("multi_target_mode", "快速一个"))
    if multi_target_mode == "最佳一个":
        multi_target_mode = "快速一个"
    if host.multi_mode_combo.findText(multi_target_mode) < 0:
        multi_target_mode = "快速一个"
    host.multi_mode_combo.setCurrentText(multi_target_mode)
    host.multi_order_combo.setCurrentText(
        str(cfg.get("multi_target_order", "从上到下"))
    )
    order_b = str(cfg.get("multi_target_order_b", "不启用"))
    if host.multi_order_b_combo.findText(order_b) < 0:
        order_b = "不启用"
    host.multi_order_b_combo.setCurrentText(order_b)
    host.multi_collect_first_edit.setText(
        str(cfg.get("multi_collect_first_hit_s", "0"))
    )
    host.multi_collect_last_edit.setText(
        str(cfg.get("multi_collect_last_hit_s", "0"))
    )
    host.multi_collect_max_edit.setText(str(cfg.get("multi_collect_max_s", "0")))
    host.multi_collect_targets_edit.setText(
        str(cfg.get("multi_collect_max_targets", "0"))
    )
    host.multi_collect_partial_chk.setChecked(
        config_bool(cfg.get("multi_collect_allow_partial", True))
    )
    backend_mode = str(cfg.get("input_backend_mode", "默认"))
    if host.input_backend_combo.findText(backend_mode) < 0:
        backend_mode = "默认"
    host.input_backend_combo.setCurrentText(backend_mode)
    host.keyboard_scan_code_chk.setChecked(
        config_bool(cfg.get("keyboard_scan_code", False))
    )
    host.mouse_move_settle.setText(str(cfg.get("mouse_move_settle_s", "0")))
    host.update_multi_target_ui()

    start_parsed = parse_hotkey_text(cfg.get("hotkey_start", "F9"))
    stop_parsed = parse_hotkey_text(cfg.get("hotkey_stop", "F10"))
    host.hotkey_start_edit.setText(
        start_parsed["display"]
        if start_parsed
        else str(cfg.get("hotkey_start", "F9"))
    )
    host.hotkey_stop_edit.setText(
        stop_parsed["display"]
        if stop_parsed
        else str(cfg.get("hotkey_stop", "F10"))
    )
    host.log_controller.apply_controls(
        normalize_log_mode(cfg.get("log_mode"), cfg.get("log_level", 0)),
        cfg.get("log_custom_categories", DEFAULT_CUSTOM_LOG_CATEGORIES),
    )

    host.tm_failsafe.setChecked(config_bool(cfg.get("tm_fs", True)))
    host.tr_failsafe.setChecked(config_bool(cfg.get("tr_fs", True)))
    host.key_failsafe.setChecked(config_bool(cfg.get("key_fs", True)))

    host.log_file_chk.setChecked(config_bool(cfg.get("log_f", False)))
    host.log_ui_chk.setChecked(config_bool(cfg.get("log_ui", True)))
    host.mini_chk.setChecked(config_bool(cfg.get("mini", False)))
    host.top_chk.setChecked(config_bool(cfg.get("top", False)))
    host.run_status_chk.setChecked(config_bool(cfg.get("run_status_tip", True)))
    host.run_status_pos_combo.setCurrentText(
        str(cfg.get("run_status_pos", "右上角"))
    )
    host.click_indicator_chk.setChecked(
        config_bool(cfg.get("click_indicator", True))
    )
    host.mouse_trail_chk.setChecked(config_bool(cfg.get("mouse_trail", False)))
    host.start_step_edit.setText(str(cfg.get("start_step", "1")))
    host.loop_start_round_edit.setText(str(cfg.get("loop_start_round", "1")))
    host.loop_end_round_edit.setText(str(cfg.get("loop_end_round", "0")))
    cpu_refresh = cfg.get("cpu_refresh_interval", 2000)
    cpu_refresh_index = host.cpu_refresh_combo.findData(cpu_refresh)
    if cpu_refresh_index < 0:
        try:
            cpu_refresh_index = host.cpu_refresh_combo.findData(int(cpu_refresh))
        except (TypeError, ValueError):
            cpu_refresh_index = -1
    host.cpu_refresh_combo.setCurrentIndex(
        cpu_refresh_index if cpu_refresh_index >= 0 else 0
    )
    host.ui_scale_edit.setText(str(cfg.get("ui_scale", "100")))
    theme_mode = str(cfg.get("theme_mode", "light"))
    theme_index = host.theme_mode_combo.findData(theme_mode)
    host.theme_mode_combo.setCurrentIndex(theme_index if theme_index >= 0 else 0)
    host.window_performance_controller.apply_resource_monitor_interval()

    host.loop_combo.setCurrentText(str(cfg.get("loop_mode", "单次")))
    host.loop_val_edit.setText(str(cfg.get("loop_val", "10")))
    time_limit_stop_mode = str(
        cfg.get("time_limit_stop_mode", "finish_loop")
    )
    time_limit_stop_index = host.time_limit_stop_mode_combo.findData(
        time_limit_stop_mode
    )
    host.time_limit_stop_mode_combo.setCurrentIndex(
        time_limit_stop_index if time_limit_stop_index >= 0 else 0
    )
    host.apply_scan_region_config(cfg.get("scan_region"), cfg.get("scan_regions"))
    host.mapping_mode_chk.setChecked(
        config_bool(cfg.get("mapping_mode_enabled", False))
    )
    host.mapping_click_mode_combo.setCurrentText(
        str(cfg.get("mapping_click_mode", "真实鼠标点击"))
    )
    host.step_adjust_follow_chk.setChecked(
        config_bool(cfg.get("step_adjust_follow", True))
    )
    host.sound_alert_audio_path_edit.setText(
        str(cfg.get("sound_alert_audio_path", ""))
    )
    host.sound_alert_play_seconds_edit.setText(
        str(cfg.get("sound_alert_play_seconds", 30))
    )
    host.apply_key_mappings_config(
        cfg.get("key_mappings", []), cfg.get("key_mapping_count", None)
    )

    if apply_tasks:
        host.close_all_task_config_dialogs()
        tasks = cfg.get("tasks", [])
        # Pre-build row list to validate data before clearing the visible list.
        new_rows = list(tasks)
        host.task_list.clear()
        for d in new_rows:
            host.add_row(
                d,
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        host.update_indexes()
        host.task_list.schedule_virtual_refresh(immediate=True)

    host.log_controller.update_global_config()
    host.log_controller.refresh_privacy_ui()
    host.update_hotkeys()
    host.apply_ui_scale(host.parse_ui_scale_percent() / 100.0)


def apply_global_settings(host, cfg, *, apply_tasks=True):
    """Validate and apply one profile config to the window.

    Args:
        host: Main window host.
        cfg: Profile config mapping.
        apply_tasks: When False, apply only the settings controls. The step
            list and open step-settings dialogs are left untouched; this is the
            deferred settings dialog's first-open path (FR-20261022-201).

    Returns:
        True when applied (or when there was nothing to apply), False when the
        pre-check failed or the apply was rolled back.
    """
    try:
        prepared = validate_profile_for_ui(cfg)
    except Exception as error:
        host._last_config_apply_error = str(error)
        write_log(
            f"配置预检失败，界面未改变: {safe_error_name(error)}",
            private_message=(
                "配置预检失败，界面未改变: " + private_error_text(error)
            ),
                     category=LOG_APPLICATION,
                     critical=True,
                 )
        return False
    try:
        previous = capture_global_settings(host)
    except Exception as exc:
        # 捕获失败时的兜底**必须**反映当前界面状态。此前用一份全新的默认配置，
        # 于是随后的回滚会把用户当前的步骤整体清空——回滚本该"什么都没发生"，
        # 实际却把界面改成了另一份内容。
        logger.warning("捕获全局设置失败: %s", exc, exc_info=True)
        previous = default_profile_config()
        snapshot = getattr(host, "snapshot_tasks", None)
        if callable(snapshot):
            # 快照本身再失败就保留默认：兜底路径不能因二次失败而中断应用启动。
            with contextlib.suppress(Exception):
                previous["tasks"] = list(snapshot())
    # FR-20260811-D12-001: 调用方误传缺 tasks 键的部分配置时（validate 层会补
    # 空列表），从当前界面状态注入 tasks，防止 _apply_global_settings_unchecked
    # clear() 步骤后按空列表重建且无回滚。检查原始 cfg：显式传空列表=用户意图。
    # apply_tasks=False 时不注入：该模式根本不会读取 tasks。
    if apply_tasks and "tasks" not in cfg:
        prepared["tasks"] = list(previous.get("tasks", []))
    try:
        _apply_global_settings_unchecked(host, prepared, apply_tasks=apply_tasks)
    except Exception as error:
        rollback_error = None
        try:
            _apply_global_settings_unchecked(host, previous, apply_tasks=apply_tasks)
        except Exception as caught:
            rollback_error = caught
        host._last_config_apply_error = str(error)
        write_log(
            f"应用配置失败，已恢复原界面: {safe_error_name(error)}",
            private_message=(
                f"应用配置失败，已恢复原界面: {private_error_text(error)}"
                + (
                    "\n恢复界面时再次失败: " + private_error_text(rollback_error)
                    if rollback_error is not None
                    else ""
                )
            ),
                     category=LOG_APPLICATION,
                     critical=True,
                 )
        return False
    host._last_config_apply_error = ""
    return True


def apply_settings_controls(host, cfg):
    """Apply only the settings-dialog controls, never the step list.

    FR-20261022-201: the deferred settings dialog's first open used the
    "load a profile" applier, which clears and rebuilds ``task_list`` from the
    saved config and closes every open step-settings dialog. Opening a settings
    window must not destroy unsaved step edits, so this entry reuses the same
    control statements with ``apply_tasks=False``.

    Returns:
        True when applied, False when the pre-check failed or was rolled back.
    """
    return apply_global_settings(host, cfg, apply_tasks=False)
