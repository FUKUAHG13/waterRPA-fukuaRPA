# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""UI scaling, resource monitoring and live move/resize throttling."""

from __future__ import annotations

import logging
import os
import time
import weakref
from ctypes import wintypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .host_view import HostView

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from PySide6.QtWidgets import QApplication, QWidget

from ...log_policy import (
    LOG_APPLICATION,
)
from ...logging_service import write_log
from ...task_model import parse_float_text
from ...win32_api import (
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOOWNERZORDER,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    kernel32,
    user32,
)
from ..components import ResponsiveRow
from ..lifecycle import schedule_widget_callback
from ..theme import (
    apply_layout_density_scale,
    apply_modern_theme,
    apply_widget_geometry_scale,
    normalize_theme_mode,
    window_visual_density,
)

logger = logging.getLogger(__name__)
class WindowPerformanceController:
    """Keep expensive UI maintenance out of the window event handlers."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.current_process = None
        self.current_process_pid = os.getpid()
        self._last_resource_sample = {
            "logical_count": os.cpu_count() or 0,
            "system_usage": "--",
            "process_usage": "--",
            "source_label": "本程序",
            "target_pid": self.current_process_pid,
        }
        if HAS_PSUTIL:
            try:
                self.current_process = psutil.Process(self.current_process_pid)
                psutil.cpu_percent(interval=None)
                self.current_process.cpu_percent(interval=None)
            except Exception as exc:
                logger.debug("初始化 CPU 采样失败: %s", exc, exc_info=True)
                self.current_process = None

    def _resource_process_target(self):
        controller = getattr(self.host, "run_controller", None)
        if controller is not None:
            try:
                return controller.resource_process_target()
            except (AttributeError, RuntimeError):
                pass
        return os.getpid(), "本程序"

    def _process_sampler(self, target_pid):
        if target_pid is None:
            self.current_process = None
            self.current_process_pid = None
            return None
        target_pid = int(target_pid)
        if (
            self.current_process is not None
            and self.current_process_pid == target_pid
        ):
            return self.current_process
        self.current_process = None
        self.current_process_pid = target_pid
        try:
            process = psutil.Process(target_pid)
            process.cpu_percent(interval=None)
        except Exception as exc:
            logger.debug("创建进程采样失败: %s", exc, exc_info=True)
            return None
        self.current_process = process
        return process

    @staticmethod
    def _resource_tooltip(source_label, target_pid, logical_count):
        source = (
            f"当前采样：{source_label}（PID {target_pid}）。"
            if target_pid is not None
            else f"当前采样：{source_label} 正在启动，尚未取得进程信息。"
        )
        return (
            "程序 CPU 保留单核口径：占满一个逻辑核心约为 100%，"
            "使用多个核心时可能超过 100%。这样便于判断单线程识别是否遇到瓶颈。\n"
            f"系统共有 {logical_count or '--'} 个逻辑处理器；"
            "显示值是最近一次刷新间隔内的平均值，不代表某一瞬间固定运行在哪个核心。\n"
            f"{source}"
        )

    def refresh_resource_label_for_mode(self):
        """Render the cached sample without perturbing psutil's interval state."""

        host = self.host
        if self.current_cpu_interval() <= 0:
            host.cpu_label.setText("资源监测: 已关闭")
            self.refresh_main_window_minimum_width()
            return
        sample = self._last_resource_sample
        source_label = sample["source_label"]
        process_usage = sample["process_usage"]
        current_mode = getattr(host, "current_settings_mode", None)
        mode = current_mode() if callable(current_mode) else "simple"
        if mode == "advanced":
            host.cpu_label.setText(
                f"系统 CPU: {sample['system_usage']}% | "
                f"{source_label} CPU: {process_usage}%"
            )
        else:
            host.cpu_label.setText(f"{source_label} CPU: {process_usage}%")
        host.cpu_label.setToolTip(
            self._resource_tooltip(
                source_label,
                sample["target_pid"],
                sample["logical_count"],
            )
        )
        self.refresh_main_window_minimum_width()

    def refresh_main_window_minimum_width(self):
        """Keep every fixed command row intact at the window's narrow edge."""

        host = self.host
        central = host.centralWidget()
        central_layout = central.layout() if central is not None else None
        horizontal_margins = 0
        if central_layout is not None:
            central_layout.activate()
            margins = central_layout.contentsMargins()
            horizontal_margins = margins.left() + margins.right()

        required_content_width = 0
        for widget_name in ("profile_bar", "command_bar", "status_bar"):
            widget = getattr(host, widget_name, None)
            if widget is None:
                continue
            layout = widget.layout()
            if layout is not None:
                layout.activate()
            required_content_width = max(
                required_content_width,
                widget.minimumSizeHint().width(),
            )

        run_layout = getattr(host, "run_controls_layout", None)
        if run_layout is not None:
            run_layout.activate()
            required_content_width = max(
                required_content_width,
                run_layout.minimumSize().width(),
            )

        scale = float(getattr(host, "ui_scale", 1.0) or 1.0)
        density = window_visual_density(host)
        baseline = max(1, int(round(760 * scale * density)))
        required = max(
            baseline,
            required_content_width + horizontal_margins,
        )
        if host.minimumWidth() != required:
            host.setMinimumWidth(required)
        return required

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已销毁")
        return host

    def current_cpu_interval(self):
        combo = getattr(self.host, "cpu_refresh_combo", None)
        selected = combo.currentData() if combo is not None else 2000
        try:
            return max(0, int(selected))
        except (TypeError, ValueError):
            return 2000

    def apply_resource_monitor_interval(self, *_):
        host = self.host
        if not getattr(host, "cpu_timer", None):
            return
        interval = self.current_cpu_interval()
        if interval <= 0:
            host.cpu_timer.stop()
            host.cpu_label.setText("资源监测: 已关闭")
            self.refresh_main_window_minimum_width()
        else:
            host.cpu_timer.setInterval(interval)
            if (
                not host._live_window_interaction
                and not host.cpu_timer.isActive()
            ):
                host.cpu_timer.start()
            self.refresh_resource_label_for_mode()

    def parse_ui_scale_percent(self):
        host = self.host
        edit = getattr(host, "ui_scale_edit", None)
        raw = (edit.text() if edit is not None else "100").strip().replace("%", "")
        value = parse_float_text(raw, 100.0)
        value = max(75.0, min(180.0, value))
        text = str(int(round(value))) if abs(value - round(value)) < 0.01 else f"{value:.1f}".rstrip("0").rstrip(".")
        scale_edit = getattr(host, "ui_scale_edit", None)
        if (
            scale_edit
            and scale_edit.text().strip().replace("%", "") != text
        ):
            scale_edit.setText(text)
        return value

    def apply_ui_scale_from_edit(self, *_):
        host = self.host
        percent = self.parse_ui_scale_percent()
        self.apply_ui_scale(percent / 100.0)
        if not host.is_switching_profile:
            host.log_setting_change("界面倍率(%)", host.ui_scale_edit.text())

    def current_theme_mode(self):
        combo = getattr(self.host, "theme_mode_combo", None)
        selected = combo.currentData() if combo is not None else "light"
        return normalize_theme_mode(selected)

    def apply_theme_mode(self, *_):
        host = self.host
        application = QApplication.instance()
        if application is None:
            return
        mode = self.current_theme_mode()
        apply_modern_theme(
            application,
            float(getattr(host, "ui_scale", 1.0)),
            mode,
        )
        self.refresh_main_window_minimum_width()
        if not host.is_switching_profile:
            host.log_setting_change(
                "界面主题",
                "深色" if mode == "dark" else "浅色",
            )

    @staticmethod
    def apply_ui_scale_to_widgets(widgets, scale):
        apply_widget_geometry_scale(widgets, scale)

    def apply_ui_scale_to_widget(self, widget):
        if not widget:
            return
        scale = float(getattr(self.host, "ui_scale", 1.0))
        widgets = [widget] + widget.findChildren(QWidget)
        apply_layout_density_scale(widget, window_visual_density(widget))
        self.apply_ui_scale_to_widgets(widgets, scale)

    def on_per_screen_visual_density_changed(self, density):
        """Refresh main-window caches that store physical row dimensions."""

        host = self.host
        effective_scale = float(getattr(host, "ui_scale", 1.0)) * max(
            0.5, min(3.0, float(density))
        )
        task_list = getattr(host, "task_list", None)
        if task_list is not None:
            task_list.update_virtual_row_height(effective_scale)
            for index in range(task_list.count()):
                item = task_list.item(index)
                widget = task_list.itemWidget(item)
                if widget:
                    item.setSizeHint(widget.sizeHint())
        self.refresh_main_window_minimum_width()

    def apply_ui_scale(self, scale):
        host = self.host
        scale = max(0.75, min(1.8, float(scale)))
        host.ui_scale = scale
        application = QApplication.instance()
        current_theme_scale = (
            float(application.property("_fukua_ui_scale") or 1.0)
            if application is not None
            else scale
        )
        current_theme_mode = (
            normalize_theme_mode(application.property("_fukua_theme_mode"))
            if application is not None
            else self.current_theme_mode()
        )
        target_theme_mode = self.current_theme_mode()
        if (
            application is not None
            and (
                abs(current_theme_scale - scale) > 0.001
                or current_theme_mode != target_theme_mode
            )
        ):
            apply_modern_theme(application, scale, target_theme_mode)
        try:
            # NOTE (L-33): scaling intentionally still walks QApplication.allWidgets()
            # rather than only the controlled roots (host, settings_dialog and the
            # task_config_dialogs set that end_live_window_interaction flushes).
            # Several transient top-level widgets that need scaling are NOT tracked
            # in that root set -- e.g. the non-modal mapping window inspector,
            # coordinate preview overlays, coordinate/region pickers and capture
            # dialogs -- so restricting the iteration would leave any of them that
            # happen to be open at scale-change time stuck at the old scale (a
            # visible regression). allWidgets() keeps coverage complete, and the
            # per-widget filter inside apply_ui_scale_to_widgets already skips
            # irrelevant widgets, so the extra iteration is cheap.
            self.apply_ui_scale_to_widgets(
                QApplication.allWidgets(), scale
            )
            host.updateGeometry()
            if getattr(host, "settings_dialog", None):
                host.settings_dialog.updateGeometry()
            self.on_per_screen_visual_density_changed(
                window_visual_density(host)
            )
        except Exception as exc:
            logger.debug("应用缩放到控件失败: %s", exc, exc_info=True)

    def begin_live_window_interaction(self, kind="move_or_resize"):
        host = self.host
        kind = str(kind or "move_or_resize")
        if host._live_window_interaction:
            host._live_window_interaction_kind = kind
            host.ui_performance.set_gauge(
                "ui.live_interaction.kind", kind
            )
            return
        host._live_window_interaction = True
        host._live_window_interaction_kind = kind
        host._live_window_interaction_started_ns = time.perf_counter_ns()
        host.ui_performance.increment("ui.live_interaction.starts")
        host.ui_performance.set_gauge("ui.live_interaction.active", True)
        host.ui_performance.set_gauge("ui.live_interaction.kind", kind)

        cpu_timer = getattr(host, "cpu_timer", None)
        host._cpu_timer_was_active = bool(
            cpu_timer and cpu_timer.isActive()
        )
        if host._cpu_timer_was_active:
            cpu_timer.stop()
        autosave_timer = getattr(host, "profiles_autosave_timer", None)
        host._autosave_timer_was_active = bool(
            autosave_timer and autosave_timer.isActive()
        )
        if host._autosave_timer_was_active:
            autosave_timer.stop()

    def end_live_window_interaction(self):
        host = self.host
        if (
            host._native_window_interaction
            or not host._live_window_interaction
        ):
            return
        host._live_window_interaction = False
        host.ui_performance.increment("ui.live_interaction.finishes")
        host.ui_performance.set_gauge("ui.live_interaction.active", False)
        if host._live_window_interaction_started_ns:
            host.ui_performance.observe_since(
                "ui.live_interaction.duration",
                host._live_window_interaction_started_ns,
            )
        host._live_window_interaction_started_ns = 0

        responsive_roots = [host, getattr(host, "settings_dialog", None)]
        responsive_roots.extend(
            getattr(host, "task_config_dialogs", set())
        )
        for root in responsive_roots:
            if root is None:
                continue
            for row in root.findChildren(ResponsiveRow):
                row.flush_height_sync()
        task_list = getattr(host, "task_list", None)
        if task_list is not None:
            task_list.schedule_virtual_refresh(immediate=True)

        cpu_timer = getattr(host, "cpu_timer", None)
        cpu_interval = self.current_cpu_interval()
        if host._cpu_timer_was_active and cpu_timer and cpu_interval > 0:
            cpu_timer.start(cpu_interval)
            schedule_widget_callback(
                host, 100, lambda owner: owner.update_cpu_info()
            )
        autosave_timer = getattr(host, "profiles_autosave_timer", None)
        if host._autosave_timer_was_active and autosave_timer:
            autosave_timer.start(3000)
        host._cpu_timer_was_active = False
        host._autosave_timer_was_active = False
        host.feedback_controller.flush_deferred_running_status()

    def schedule_live_interaction_idle(self, kind):
        host = self.host
        if not host.isVisible() or host._native_window_interaction:
            return
        self.begin_live_window_interaction(kind)
        host._live_interaction_idle_timer.start(140)

    def update_cpu_info(self):
        host = self.host
        if host._live_window_interaction:
            host.ui_performance.increment(
                "ui.live_interaction.resource_refresh_skipped"
            )
            return
        started_ns = time.perf_counter_ns()
        logical_count = os.cpu_count() or 0
        sys_usage = "--"
        proc_usage = "--"
        target_pid, source_label = self._resource_process_target()
        if HAS_PSUTIL:
            try:
                logical_count = (
                    psutil.cpu_count(logical=True) or logical_count
                )
                sys_usage = f"{psutil.cpu_percent(interval=None):.1f}"
                process = self._process_sampler(target_pid)
                if process is not None:
                    raw_usage = process.cpu_percent(interval=None)
                    # DESIGN: psutil's process value is intentionally kept in
                    # single-logical-core units. Do not divide by CPU count;
                    # users need ~100% to expose a saturated recognition core.
                    proc_usage = f"{raw_usage:.1f}"
            except Exception as exc:
                logger.debug("采样进程资源失败: %s", exc, exc_info=True)
                pass
        self._last_resource_sample = {
            "logical_count": logical_count,
            "system_usage": sys_usage,
            "process_usage": proc_usage,
            "source_label": source_label,
            "target_pid": target_pid,
        }
        self.refresh_resource_label_for_mode()
        host.ui_performance.observe_since(
            "ui.resource_monitor.refresh", started_ns
        )

    # ------------------------------------------------------------------
    # Window topmost toggle (migrated from RPAWindow)
    # ------------------------------------------------------------------

    def toggle_top_window(self):
        """使用 Win32 API 切换置顶，避免 setWindowFlags 重建窗口导致标题栏异常。"""
        host = self.host
        should_be_top = host.top_chk.isChecked()
        hwnd = wintypes.HWND(int(host.winId()))
        insert_after = HWND_TOPMOST if should_be_top else HWND_NOTOPMOST
        swp_flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER

        if host.isVisible():
            swp_flags |= SWP_SHOWWINDOW

        if not user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, swp_flags):
            err = kernel32.GetLastError()
            write_log(f"切换窗口置顶失败: WinError {err}",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
