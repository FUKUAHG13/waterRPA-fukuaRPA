# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false

"""Key-mapping rows, target-window binding and click dispatch."""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QToolTip

from ...log_policy import (
    LOG_ACTION,
)
from ...logging_service import write_log
from ...mapping_backend import WindowMappingBackend
from ...privacy import private_error_text, safe_error_name
from ...task_model import parse_coordinate_text
from ...win32_api import (
    HOTKEY_ID_MAPPING_BASE,
    is_safe_global_hotkey,
    parse_hotkey_text,
)
from .mapping_mixins import MappingBindingMixin, MappingHotkeyCaptureMixin, MappingRowMixin

if TYPE_CHECKING:
    from .host_view import HostView



class MappingController(MappingBindingMixin, MappingHotkeyCaptureMixin, MappingRowMixin):
    """Own mapping backend and transient picker/inspector lifecycles."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.backend = None
        self.pickers = {}
        self.inspectors = set()
        self.bulk_update = False

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("按键映射窗口已销毁")
        return host

    def settings_interaction_parent(self):
        # FR-20260812-D10-b: 回退分支必须返回 QWidget（供 QMessageBox 等作 parent）。
        # 此前回退返回 self（MappingController，非 QObject），异步回调（如
        # diagnostics :198/:269 信号槽）在对话框关闭后仍会触发该分支，向 QDialog
        # 传非 QWidget parent 会抛 TypeError。
        dialog = getattr(self.host, "settings_dialog", None)
        if dialog is not None and dialog.isVisible():
            return dialog
        main_window = getattr(self.host, "window", None)
        if callable(main_window):
            parent = main_window()
            if parent is not None and hasattr(parent, "isVisible"):
                return parent
        return self.host

    def restore_settings_dialog_focus(self):
        dialog = getattr(self.host, "settings_dialog", None)
        if dialog is None or not dialog.isVisible():
            return
        dialog.raise_()
        dialog.activateWindow()

    def ensure_mapping_backend(self):
        if self.backend is None:
            self.backend = WindowMappingBackend()
        return self.backend

    def hwnd_class_name(self, hwnd):
        return self.host.ensure_mapping_backend().class_name(hwnd)

    def hwnd_title(self, hwnd):
        return self.host.ensure_mapping_backend().title(hwnd)

    def hwnd_process_info(self, hwnd):
        return self.host.ensure_mapping_backend().process_info(hwnd)

    def hwnd_belongs_to_current_process(self, hwnd):
        return self.host.ensure_mapping_backend().belongs_to_current_process(hwnd)

    def hwnd_screen_area_at_point(self, hwnd, x, y):
        return self.host.ensure_mapping_backend().screen_area_at_point(hwnd, x, y)

    def top_level_window_at_point(self, x, y, exclude_current_process=False):
        return self.host.ensure_mapping_backend().top_level_window_at_point(
            x, y, exclude_current_process=exclude_current_process
        )

    def active_key_mappings(self):
        mappings = []
        for idx, row in enumerate(getattr(self.host, "key_mapping_rows", [])):
            if not row["enabled"].isChecked():
                continue
            coord = parse_coordinate_text(row["coord"].text())
            if not coord:
                continue
            parsed = parse_hotkey_text(row["hotkey"].text())
            if not parsed:
                continue
            mappings.append({
                "index": idx,
                "id": HOTKEY_ID_MAPPING_BASE + idx,
                "hotkey": parsed["display"],
                "normalized": parsed["text"],
                "vk": parsed["vk"],
                "modifiers": parsed["modifiers"],
                "bare": parsed["bare"],
                "safe_global": is_safe_global_hotkey(parsed),
                "coord": coord,
                "action": row["action"].currentText(),
                "window_binding": dict(row.get("window_binding", {})),
            })
        return mappings

    def _input_block_reason(self):
        """输入所有权判据：复用 ``ExecutionController.launch_blocked_reason``。

        FR-001：裸键映射的 Hook 分发路径此前没有任何所有权门，连点器运行期间
        仍会注入真实点击（与注册热键路径不一致）。

        DESIGN：这里**不另写一份判据**。ADV-20261006-002 已把该判据从"写了四
        遍"收拢到 ``launch_blocked_reason``，理由是每份副本都会漏项——第四份
        （``prepare_and_launch_profile``）当时就漏了识别测试这一项。裸键映射与
        脚本启动争用同一套输入后端，必须走同一个判据。
        """
        launch_blocked = getattr(self.host, "controller", None)
        reason_getter = getattr(launch_blocked, "launch_blocked_reason", None)
        if callable(reason_getter):
            reason = reason_getter()
            if reason:
                return reason
            # 空串表示"已阻止但无需打扰用户"（脚本已在运行）。对按键映射而言
            # 同样不该注入点击，但也不必弹窗打断。
            return "已有脚本或连点器正在运行"
        # host 未暴露 controller（独立使用该控制器的场景）时退回到最小判据。
        # 仍须与 launch_blocked_reason 同口径地覆盖三项：连点器、脚本会话
        # （run_controller.is_active —— "会话活跃但引擎空闲"是可达状态，见
        # Runner 收尾窗口）、识别测试。
        if bool(getattr(self.host, "engine", None) and self.host.engine.is_running):
            return "脚本正在运行"
        run_controller = getattr(self.host, "run_controller", None)
        if run_controller is not None and bool(
            getattr(run_controller, "is_active", False)
        ):
            return "脚本会话正在运行"
        feedback = getattr(self.host, "feedback_controller", None)
        if feedback is not None and bool(
            getattr(feedback, "recognition_test_active", False)
        ):
            return "识别测试正在运行"
        checker = getattr(self.host, "auto_clicker_is_running", None)
        if checker and checker() is True:
            return "连点器正在运行，请先停止连点器"
        return ""

    def execute_key_mapping_by_hotkey(self, hotkey_text):
        parsed = parse_hotkey_text(hotkey_text)
        if not parsed:
            return
        # FR-001：输入已被占用时不得执行裸键映射。拒绝必须留痕（与注册热键
        # 路径一致），否则用户按下 F5 却毫无反应，无法判断是映射未命中还是
        # 被占用拒绝。
        reason = self._input_block_reason()
        if reason:
            self.host.append_application_log(
                f"<font color='orange'>按键映射 {hotkey_text} 本次未执行："
                f"{reason}。</font>"
            )
            return
        for item in self.host.active_key_mappings():
            if item.get("normalized") == parsed["text"]:
                self.host.execute_key_mapping(item["index"])
                return

    def current_mapping_click_mode(self):
        combo = getattr(self.host, "mapping_click_mode_combo", None)
        if not combo:
            return "真实鼠标点击"
        text = combo.currentText()
        return text if text else "真实鼠标点击"

    def perform_mapping_mouse_click(self, x, y, button, click_times, restore_position=False):
        return self.host.ensure_mapping_backend().foreground_click(
            x, y, button, click_times, restore_position=restore_position
        )

    def background_click_target_hwnd(
        self, x, y, expected_root=None, exclude_current_process=False
    ):
        return self.host.ensure_mapping_backend().background_click_target(
            x,
            y,
            expected_root=expected_root,
            exclude_current_process=exclude_current_process,
            belongs_callback=self.host.hwnd_belongs_to_current_process,
            top_level_callback=self.host.top_level_window_at_point,
            area_callback=self.host.hwnd_screen_area_at_point,
        )

    def window_matches_binding(self, hwnd, binding):
        return self.host.ensure_mapping_backend().matches_binding(hwnd, binding)

    def resolve_mapping_window_binding(self, binding):
        return self.host.ensure_mapping_backend().resolve_binding(binding)

    def perform_mapping_background_click(self, binding, button, click_times):
        return self.host.ensure_mapping_backend().background_click(binding, button, click_times)

    def perform_key_mapping_click(self, x, y, button, click_times, window_binding=None):
        mode = self.host.current_mapping_click_mode()
        if mode == "点击后返回原位":
            self.host.perform_mapping_mouse_click(x, y, button, click_times, restore_position=True)
            return mode
        if mode == "后台窗口点击(实验)":
            if self.host.perform_mapping_background_click(window_binding, button, click_times):
                method = str(
                    self.host.ensure_mapping_backend().last_background_method
                    or "窗口消息"
                )
                return f"{mode} · {method}"
            return f"{mode}失败，未执行点击"
        self.host.perform_mapping_mouse_click(x, y, button, click_times, restore_position=False)
        return mode

    def execute_key_mapping(self, map_idx):
        if self.host.engine.is_running:
            self.host.append_application_log("<font color='gray'>脚本正在运行，已忽略按键映射，避免打断自动化流程。</font>")
            return
        mapping = None
        for item in self.host.active_key_mappings():
            if item["index"] == map_idx:
                mapping = item
                break
        if not mapping:
            return
        x, y = mapping["coord"]
        action = mapping["action"]
        button = "right" if "右键" in action else "left"
        click_times = 2 if "双击" in action else 1
        try:
            click_mode = self.host.perform_key_mapping_click(x, y, button, click_times, mapping.get("window_binding"))
            if "失败" in click_mode:
                self.host.append_application_log(f"<font color='orange'>按键映射{map_idx + 1} 后台点击失败，未执行任何点击；请重新绑定目标窗口或改用其他点击方式。</font>")
                QToolTip.showText(QCursor.pos(), "后台点击失败，未执行点击")
                return
            if getattr(self.host, "click_indicator_chk", None) is None or self.host.click_indicator_chk.isChecked():
                self.host.show_click_indicator_overlay({"x": x, "y": y, "text": f"映射{map_idx + 1}"})
            self.host.append_application_log(
                f"<font color='gray'>按键映射{map_idx + 1} 已执行：{action} - {click_mode}</font>",
                private_msg=(
                    f"<font color='gray'>按键映射{map_idx + 1} 已执行："
                    f"{action} ({x},{y}) - {click_mode}</font>"
                ),
            )
        except Exception as e:
            write_log(
                f"执行按键映射失败: {safe_error_name(e)}",
                private_message=f"执行按键映射失败: {private_error_text(e)}",
                         category=LOG_ACTION,
                         critical=True,
                     )
