# pyright: reportAttributeAccessIssue=false
"""连点器工具窗口的启动/停止生命周期 Mixin（God Class 拆分）。

从 ``AutoClickerToolWindow`` 提取的运行生命周期方法组：脚本互斥检查、
参数收集、启停入口与工作线程回调。窗口构造、布局与控件仍归主类所有，
本 Mixin 不持有任何状态。
"""

from __future__ import annotations

import contextlib

from ...constants import TASK_TYPE_AUTO_CLICKER
from ..auto_clicker_core import _SPEED_PRESETS, _AutoClickerRunner


class RunnerLifecycleMixin:
    """连点器启动/停止与工作线程生命周期（8 方法）。"""

    def _script_is_active(self):
        """Check the shared script runner without coupling to one backend."""
        host = self._host_ref()
        controller = getattr(host, "run_controller", None)
        if controller is not None:
            return bool(controller.is_active)
        lifecycle = getattr(self.engine, "lifecycle", None)
        return bool(lifecycle is not None and lifecycle.is_active)

    def _collect_task(self) -> dict:
        speed_idx = self.speed_combo.currentIndex()
        if speed_idx < len(_SPEED_PRESETS) - 1:
            _, interval_base, _interval_max = _SPEED_PRESETS[speed_idx]
            interval_random_enabled = False
            interval_variation = 0.0
            interval_concentration = 0
        else:
            interval_base = self.interval_base_spin.value()
            interval_random_enabled = self.chk_random_interval.isChecked()
            interval_variation = (
                self.interval_variation_spin.value()
                if interval_random_enabled
                else 0.0
            )
            interval_concentration = self.interval_concentration.value()

        center_mode = "fixed" if self.chk_fixed_center.isChecked() else "current"

        jitter_enabled = self.chk_jitter.isChecked()
        jitter_h = self.jitter_x_spin.value() if jitter_enabled else 0
        jitter_v = self.jitter_y_spin.value() if jitter_enabled else 0

        curve = 0.0
        if self.chk_curve.isChecked():
            curve = self.curve_strength_spin.value()

        return {
            "type": TASK_TYPE_AUTO_CLICKER,
            "ac_button": self.btn_type_combo.currentData() or "left",
            "ac_center_mode": center_mode,
            "ac_click_count": str(self.click_count_min.value()),
            "ac_click_count_min": str(self.click_count_min.value()),
            "ac_click_count_max": str(self.click_count_max.value()),
            "ac_big_loop_count": str(self.big_loop_min.value()),
            "ac_big_loop_min": str(self.big_loop_min.value()),
            "ac_big_loop_max": str(self.big_loop_max.value()),
            "ac_sub_loop_min": str(self.sub_loop_min.value()),
            "ac_sub_loop_max": str(self.sub_loop_max.value()),
            "ac_interval_base": str(interval_base),
            "ac_interval_random_en": interval_random_enabled,
            "ac_interval_variation": str(interval_variation),
            "ac_interval_concentration": str(interval_concentration),
            # Legacy fields remain internally coherent for the hidden legacy
            # command path, while the standalone tool uses the richer fields.
            "ac_interval_min": str(max(0.0, interval_base - interval_variation)),
            "ac_interval_max": str(interval_base + interval_variation),
            "ac_press_duration": str(self.hold_min.value() / 1000.0),
            "ac_press_duration_min": str(self.hold_min.value() / 1000.0),
            "ac_press_duration_max": str(self.hold_max.value() / 1000.0),
            "ac_big_loop_interval": str(self.big_interval_min.value() / 1000.0),
            "ac_big_interval_min": str(self.big_interval_min.value() / 1000.0),
            "ac_big_interval_max": str(self.big_interval_max.value() / 1000.0),
            "ac_sub_interval_min": str(self.sub_interval_min.value() / 1000.0),
            "ac_sub_interval_max": str(self.sub_interval_max.value() / 1000.0),
            "ac_fixed_x": str(self.fixed_x_spin.value()),
            "ac_fixed_y": str(self.fixed_y_spin.value()),
            "ac_jitter_h": str(jitter_h),
            "ac_jitter_v": str(jitter_v),
            "ac_jitter_shape": self.jitter_shape_combo.currentData() or "rectangle",
            "ac_jitter_concentration": str(self.jitter_concentration.value()),
            "ac_move_curve_strength": str(curve),
        }

    def request_start(self):
        """Start through the same checked path used by the visible button."""
        self._on_start()
        return self.is_running

    def _on_start(self):
        if self.engine is None:
            self.status_label.setText("引擎不可用")
            return
        if self._script_is_active():
            self.status_label.setText("脚本运行中，无法启动连点器")
            return
        if self.is_running:
            return  # 已在运行
        self._stop_event.clear()
        self._thread_error_occurred = False  # FR-20260811-D13-001: 复位错误标志
        task = self._collect_task()
        self._thread = _AutoClickerRunner(self.engine, task, self._stop_event)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.error.connect(self._on_thread_error)
        self._thread.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.fixed_pick_btn.setEnabled(False)
        self._set_banner_state("running")
        self._update_banner()
        self.status_label.setText("运行中...")
        host = self._host_ref()
        status_checkbox = getattr(host, "run_status_chk", None)
        if host is not None and (
            status_checkbox is None or status_checkbox.isChecked()
        ):
            position_combo = getattr(host, "run_status_pos_combo", None)
            position = (
                position_combo.currentText()
                if position_combo is not None
                else "右上角"
            )
            host.show_running_status_overlay(position, "连点器")
            host.update_running_status_overlay(
                {
                    "cmd": f"{self.btn_type_combo.currentText()}连点",
                    "detail": f"按 {self.stop_key_edit.text().strip() or '停止键'} 停止",
                }
            )

    def _on_stop(self):
        self.request_stop()

    def request_stop(self):
        """Request only the tool-local worker to stop."""
        self._stop_event.set()
        self.status_label.setText("正在停止...")

    def toggle_from_hotkey(self):
        """Same-key start/stop toggle used by the global hotkey backend.

        空闲：启动；运行或启动中：请求停止（取消尚未真正开始的启动）；
        停止中：忽略重复按下，等线程真正结束后下一次按键才允许重新启动。
        任何状态下都不排队启动第二个实例。
        """
        if self._thread is not None:
            if not self._stop_event.is_set():
                self.request_stop()
            return True
        self._on_start()
        return self._thread is not None

    def _on_thread_finished(self):
        # FR-20260809-1502: 按 sender 识别线程。旧线程的 finished 信号若在热键
        # 启动新线程之后才被处理（竞态窗口），不得执行任何 UI 重置或清理（否则
        # 运行中的新线程界面被置为停止态、_thread 被误删）。sender 为 None 时
        # （直接调用/测试）按原语义处理。
        sender = self.sender()
        if sender is not None and sender is not self._thread:
            return
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.fixed_pick_btn.setEnabled(True)
        # FR-20260811-D13-001: 线程以错误结束时（_on_thread_error 已置位），保留
        # "运行出错"文本与 error 样式，跳过 _update_banner 的文本重建（它会按
        # isRunning() 覆盖为"按 X 开始"）；否则错误详情对用户丢失。
        if not getattr(self, "_thread_error_occurred", False):
            self.status_label.setText("已停止")
            self._set_banner_state("idle")
            self._update_banner()
        host = self._host_ref()
        if host is not None:
            host.hide_running_status_overlay("连点器")
        if self._thread is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                self._thread.deleteLater()
            self._thread = None
        self._thread_error_occurred = False  # 复位供下次启动

    def _on_thread_error(self, message: str):
        # FR-20260811-D13-001: 与 _on_thread_finished 的 sender 防护对称——旧线程
        # error 信号迟到（热键已启动新线程）时不得 set 停止事件误停新线程。
        sender = self.sender()
        if sender is not None and sender is not self._thread:
            return
        self._stop_event.set()
        self._thread_error_occurred = True
        self.status_label.setText(f"运行出错: {message}")
        self.banner_label.setText("运行出错")
        self._set_banner_state("error")
