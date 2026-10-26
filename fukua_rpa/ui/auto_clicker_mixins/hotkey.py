# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""连点器工具窗口的全局快捷键 Mixin（God Class 拆分）。

从 ``AutoClickerToolWindow`` 提取的快捷键方法组：设置读取、
KeyCaptureDialog 录入、全局热键后端刷新与启停横幅文案。
快捷键控件（``start_key_edit``/``stop_key_edit``）由主类视图构建持有。
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLineEdit, QMessageBox

from ...win32_api import (
    hotkey_signature,
    is_safe_global_hotkey,
    parse_hotkey_text,
)
from ..auto_clicker_core import _START_HOTKEY_KEY, _STOP_HOTKEY_KEY


class HotkeyMixin:
    """全局启动/停止快捷键与启停横幅（5 方法）。"""

    @staticmethod
    def _load_hotkey(settings, key, fallback):
        text = str(
            settings.value(key, fallback)
            if settings is not None
            else fallback
        )
        parsed = parse_hotkey_text(text)
        if parsed and is_safe_global_hotkey(parsed):
            return str(parsed["display"])
        return fallback

    def _capture_hotkey(self, target_edit: QLineEdit):
        """打开 KeyCaptureDialog 录入快捷键，结果写入 target_edit。"""
        from ..input_tools import KeyCaptureDialog

        dlg = KeyCaptureDialog(self)
        if dlg.exec() == QDialog.Accepted:
            text = dlg.result_text if hasattr(dlg, "result_text") else ""
            if not text:
                text = dlg.captured_text if hasattr(dlg, "captured_text") else ""
            if text:
                parsed = parse_hotkey_text(text)
                if not parsed or not is_safe_global_hotkey(parsed):
                    QMessageBox.warning(
                        self,
                        "快捷键不适合全局启停",
                        "请使用 F1-F12 等功能键，或带 Ctrl/Alt/Shift/Win 的组合键。",
                    )
                    return
                host = self._host_ref()
                controller = getattr(host, "hotkey_controller", None)
                reserved = (
                    controller.reserved_hotkey_signatures(
                        include_auto_clicker=False
                    )
                    if controller is not None
                    else {}
                )
                owner = reserved.get(hotkey_signature(parsed))
                if owner:
                    QMessageBox.warning(
                        self,
                        "快捷键冲突",
                        f"该快捷键已用于“{owner}”，请换一个快捷键。",
                    )
                    return
                target_edit.setText(str(parsed["display"]))
                settings = getattr(host, "settings", None)
                if settings is not None:
                    key = (
                        _START_HOTKEY_KEY
                        if target_edit is self.start_key_edit
                        else _STOP_HOTKEY_KEY
                    )
                    settings.setValue(key, str(parsed["display"]))
                    settings.sync()
                self._register_shortcuts()

    def _register_shortcuts(self):
        """Refresh the shared Win32 global-hotkey backend."""
        self._update_banner()
        if not self._hotkeys_active:
            return
        host = self._host_ref()
        controller = getattr(host, "hotkey_controller", None)
        if controller is not None:
            controller.refresh_hotkey_backend()

    def hotkey_bindings(self):
        """Return validated global bindings while the tool window is open.

        ``toggle`` is True when start and stop share one signature; the shared
        Win32 backend then registers a single toggle hotkey instead of two.
        """

        if not self._hotkeys_active:
            return {}
        start = parse_hotkey_text(self.start_key_edit.text())
        stop = parse_hotkey_text(self.stop_key_edit.text())
        start_text = (
            str(start["display"])
            if start and is_safe_global_hotkey(start)
            else ""
        )
        stop_text = (
            str(stop["display"])
            if stop and is_safe_global_hotkey(stop)
            else ""
        )
        toggle = bool(
            start
            and stop
            and is_safe_global_hotkey(start)
            and is_safe_global_hotkey(stop)
            and hotkey_signature(start) == hotkey_signature(stop)
        )
        return {
            "start_hotkey": start_text,
            "stop_hotkey": stop_text,
            "toggle": toggle,
        }

    def _same_key_toggle(self):
        """Whether start/stop currently share one physical hotkey signature."""

        start = parse_hotkey_text(self.start_key_edit.text())
        stop = parse_hotkey_text(self.stop_key_edit.text())
        return bool(
            start and stop and hotkey_signature(start) == hotkey_signature(stop)
        )

    def _update_banner(self):
        btn_name = self.btn_type_combo.currentText()
        start_key = self.start_key_edit.text().strip() or "无"
        stop_key = self.stop_key_edit.text().strip() or "无"
        if self._thread is not None and self._thread.isRunning():
            self.banner_label.setText(f"运行中 — {btn_name}连点 · 按 {stop_key} 停止")
        elif self._same_key_toggle():
            # 同键时不再显示两条看似分别注册的热键。
            self.banner_label.setText(f"按 {start_key} 启动/停止 · {btn_name}连点")
        else:
            self.banner_label.setText(
                f"按 {start_key} 开始 · {stop_key} 停止 · {btn_name}连点"
            )
        self._adjust_size()
