# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportOptionalSubscript=false, reportGeneralTypeIssues=false, reportOptionalMemberAccess=false
"""Global control hotkeys, mapping hotkeys and bare-key hook lifecycle."""

from __future__ import annotations

import logging
import weakref
from ctypes import wintypes
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from ...log_policy import (
    LOG_APPLICATION,
)
from ...logging_service import write_log
from ...privacy import private_error_text, safe_error_name
from ...win32_api import (
    HOTKEY_ID_AUTO_CLICKER_START,
    HOTKEY_ID_AUTO_CLICKER_STOP,
    HOTKEY_ID_PROFILE_START_BASE,
    HOTKEY_ID_PROFILE_STOP_BASE,
    HOTKEY_ID_START,
    HOTKEY_ID_STOP,
    MOD_NOREPEAT,
    hotkey_is_down,
    hotkey_signature,
    is_safe_global_hotkey,
    parse_hotkey_text,
    user32,
)
from ..input_tools import KeyMappingHookThread
from ..lifecycle import schedule_widget_callback

if TYPE_CHECKING:
    from .host_view import HostView

logger = logging.getLogger(__name__)

_HOOK_STOP_TIMEOUT_MS = 1500


class HotkeyController:
    """Own registered-hotkey and polling state for one main window."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.start_parsed = parse_hotkey_text("F9")
        self.stop_parsed = parse_hotkey_text("F10")
        self.start_vk = self.start_parsed["vk"]
        self.stop_vk = self.stop_parsed["vk"]
        self.registered = False
        self.poll_pressed = set()
        self.mapping_ids = {}
        self.auto_clicker_ids = {}
        self.profile_start_ids = {}
        self.profile_stop_ids = {}
        self.mapping_poll_pressed = set()
        # REVIEW-20260913-001 FR-006: 最近一次注册中无法注册的按键映射
        # （index -> 热键文本）。注册失败必须可查，不能只留一条日志。
        self.mapping_registration_failures: dict[int, str] = {}
        self.mapping_hook = None
        self.mapping_hook_hotkeys = set()

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("热键所属窗口已销毁")
        return host

    def _script_is_running(self):
        controller = getattr(self.host, "run_controller", None)
        if controller is not None and bool(controller.is_active):
            return True
        return bool(self.host.engine.is_running)

    def _input_operation_is_running(self):
        checker = getattr(self.host, "auto_clicker_is_running", None)
        return self._script_is_running() or bool(checker and checker() is True)

    def _auto_clicker_bindings(self):
        getter = getattr(self.host, "auto_clicker_hotkey_bindings", None)
        value = getter() if callable(getter) else {}
        return value if isinstance(value, dict) else {}

    def _auto_clicker_configured_bindings(self):
        getter = getattr(
            self.host, "auto_clicker_configured_hotkey_bindings", None
        )
        value = getter() if callable(getter) else self._auto_clicker_bindings()
        return value if isinstance(value, dict) else {}

    def _profile_bindings(self):
        controller = getattr(self.host, "profile_hotkey_controller", None)
        return controller.active_bindings() if controller is not None else []

    def _profile_configured_bindings(self):
        controller = getattr(self.host, "profile_hotkey_controller", None)
        if controller is None:
            return []
        getter = getattr(controller, "configured_bindings", None)
        return getter() if callable(getter) else controller.active_bindings()

    def reserved_hotkey_signatures(
        self,
        *,
        include_profile=True,
        include_auto_clicker=True,
    ):
        """Return active global hotkey ownership for configuration validation."""

        result = {}

        def add(text, label):
            parsed = parse_hotkey_text(text)
            if parsed:
                result[hotkey_signature(parsed)] = label

        start_edit = getattr(self.host, "hotkey_start_edit", None)
        stop_edit = getattr(self.host, "hotkey_stop_edit", None)
        add(start_edit.text() if start_edit is not None else "", "脚本启动热键")
        add(stop_edit.text() if stop_edit is not None else "", "脚本停止热键")
        if include_auto_clicker:
            clicker = self._auto_clicker_configured_bindings()
            if clicker.get("toggle"):
                add(clicker.get("start_hotkey", ""), "连点器启停热键")
            else:
                add(clicker.get("start_hotkey", ""), "连点器启动热键")
                add(clicker.get("stop_hotkey", ""), "连点器停止热键")
        for mapping in self.host.active_key_mappings():
            add(mapping.get("normalized", ""), f"按键映射 {mapping.get('index', 0) + 1}")
        if include_profile:
            for binding in self._profile_configured_bindings():
                label = f"方案“{binding.get('profile_name', '')}”"
                add(binding.get("start_hotkey", ""), label + "启动热键")
                add(binding.get("stop_hotkey", ""), label + "停止热键")
        return result

    def handle_native_hotkey(self, hotkey_id):
        hotkey_id = int(hotkey_id)
        if hotkey_id == HOTKEY_ID_START and not self._input_operation_is_running():
            schedule_widget_callback(self.host, 0, lambda owner: owner.start_task())
            return True
        if hotkey_id == HOTKEY_ID_STOP and self._script_is_running():
            schedule_widget_callback(self.host, 0, lambda owner: owner.stop_task())
            return True
        clicker_action = self.auto_clicker_ids.get(hotkey_id)
        if clicker_action == "toggle":
            # 同键在空闲/运行/启动/停止四态下由工具窗口统一决策；这里不能叠加
            # _input_operation_is_running 门槛——连点器自身运行时该值为真，
            # 若在此拦截就永远无法用同键停止。
            schedule_widget_callback(
                self.host, 0, lambda owner: owner.toggle_auto_clicker_from_hotkey()
            )
            return True
        if clicker_action == "start":
            if not self._input_operation_is_running():
                schedule_widget_callback(
                    self.host, 0, lambda owner: owner.start_auto_clicker_from_hotkey()
                )
            return True
        if clicker_action == "stop":
            checker = getattr(self.host, "auto_clicker_is_running", None)
            if checker and checker() is True:
                schedule_widget_callback(
                    self.host, 0, lambda owner: owner.stop_auto_clicker()
                )
            return True
        profile_target = self.profile_start_ids.get(hotkey_id)
        if profile_target is not None:
            profile_id, slot_id = profile_target
            if not self._input_operation_is_running():
                schedule_widget_callback(
                    self.host,
                    0,
                    lambda owner, target=profile_id, slot=slot_id: (
                        owner.profile_hotkey_controller.start_profile(target, slot)
                    ),
                )
            return True
        profile_id = self.profile_stop_ids.get(hotkey_id)
        if profile_id is not None:
            schedule_widget_callback(
                self.host,
                0,
                lambda owner, target=profile_id: owner.profile_hotkey_controller.stop_profile(target),
            )
            return True
        mapping_index = self.mapping_ids.get(hotkey_id)
        if mapping_index is not None:
            # Registered and polling mappings follow the same input-ownership
            # rule. Consume the hotkey while another input task is active, but
            # do not inject a competing click or key sequence.
            if self._input_operation_is_running():
                return True
            schedule_widget_callback(
                self.host,
                0,
                lambda owner, index=mapping_index: owner.execute_key_mapping(index),
            )
            return True
        return False

    def hotkey_hwnd(self):
        return wintypes.HWND(int(self.host.winId()))

    def validate_control_hotkey(self, text, default_text, label):
        parsed = parse_hotkey_text(text)
        if parsed and is_safe_global_hotkey(parsed):
            return parsed
        fallback = parse_hotkey_text(default_text)
        if not getattr(self.host, "is_switching_profile", False):
            QMessageBox.warning(
                self.host,
                "热键设置无效",
                f"{label}不能使用裸字母、裸数字、空格或回车这类容易误触的单键。\n"
                f"请使用 Ctrl/Alt/Shift/Win 组合键，或 F1-F12 等功能键。\n"
                f"已临时恢复为 {fallback['display']}。"
            )
        return fallback

    def stop_key_mapping_hook(self):
        hook = getattr(self, "mapping_hook", None)
        if hook and hook.isRunning():
            hook.stop()
            if not hook.wait(_HOOK_STOP_TIMEOUT_MS):
                write_log("按键映射钩子未能在限定时间内停止，暂不创建新的钩子。",
                             category=LOG_APPLICATION,
                             critical=True,
                         )
                return False
        self.mapping_hook = None
        self.mapping_hook_hotkeys = set()
        return True

    def refresh_mapping_mode_hook(self):
        if getattr(self.host, "_bulk_mapping_update", False):
            return
        if not getattr(self.host, "mapping_mode_chk", None):
            return
        wanted = set()
        if self.host.mapping_mode_chk.isChecked():
            for mapping in self.host.active_key_mappings():
                if mapping.get("bare") and not mapping.get("safe_global"):
                    wanted.add(mapping.get("normalized"))
        wanted.discard(None)

        if not wanted:
            self.host.stop_key_mapping_hook()
            return
        if getattr(self, "mapping_hook", None) and self.mapping_hook.isRunning() and wanted == getattr(self, "mapping_hook_hotkeys", set()):
            return
        if not self.host.stop_key_mapping_hook():
            return
        self.mapping_hook_hotkeys = set(wanted)
        self.mapping_hook = KeyMappingHookThread(self.mapping_hook_hotkeys)
        self.mapping_hook.triggered.connect(self.host.execute_key_mapping_by_hotkey)
        self.mapping_hook.failed.connect(self._on_mapping_hook_failed)
        self.mapping_hook.start()

    def _on_mapping_hook_failed(self, reason):
        # SAFETY/INP-FR-001: 裸键映射没有轮询回退路径，安装失败必须给出可见
        # 原因，不能静默丢弃（约束 #90）；映射模式随后可由用户重试或改用组合键。
        write_log(f"按键映射模式启动失败：{reason}",
                     category=LOG_APPLICATION,
                     critical=True,
                 )

    def unregister_global_hotkeys(self):
        hwnd = self.hotkey_hwnd()
        hotkey_ids = {
            HOTKEY_ID_START,
            HOTKEY_ID_STOP,
            *getattr(self, "mapping_ids", {}).keys(),
            *getattr(self, "auto_clicker_ids", {}).keys(),
            *getattr(self, "profile_start_ids", {}).keys(),
            *getattr(self, "profile_stop_ids", {}).keys(),
        }
        for hotkey_id in hotkey_ids:
            try:
                user32.UnregisterHotKey(hwnd, hotkey_id)
            except Exception as exc:
                logger.warning("注销热键失败: %s", exc, exc_info=True)
                pass
        self.mapping_ids = {}
        self.auto_clicker_ids = {}
        self.profile_start_ids = {}
        self.profile_stop_ids = {}
        self._clear_registration_state()

    def _clear_registration_state(self) -> None:
        """Mark the controller unregistered and drop any stale mapping notice.

        ``mapping_registration_failures`` describes the *current* registration
        attempt. Carrying it past a failed or aborted one makes the UI report a
        mapping problem that no longer exists.
        """
        self.registered = False
        self.mapping_registration_failures = {}
        self._refresh_mapping_failure_notice()

    def _refresh_mapping_failure_notice(self) -> None:
        """Push the current failure state to the settings notice, if any."""
        mapping = getattr(self.host, "mapping_controller", None)
        refresh = getattr(mapping, "refresh_mapping_hotkey_failure_notice", None)
        if callable(refresh):
            refresh()

    @staticmethod
    def _register_hotkey(hwnd, hotkey_id, parsed):
        return bool(
            user32.RegisterHotKey(
                hwnd,
                int(hotkey_id),
                int(parsed["modifiers"]) | MOD_NOREPEAT,
                int(parsed["vk"]),
            )
        )

    def _register_optional_hotkeys(self, hwnd, used_hotkeys):
        """Register clicker/profile hotkeys and report external conflicts.

        Optional feature hotkeys share the same backend as script controls. If
        Windows rejects one, the caller switches the whole backend to polling;
        otherwise a visible, valid binding would silently stop working merely
        because the main F9/F10 pair registered successfully.
        """

        clicker = self._auto_clicker_bindings()
        # 同键启停（start == stop 签名）只注册一条 toggle 绑定；不同键保持原来的
        # 分别启停注册。
        if clicker.get("toggle"):
            clicker_entries = (
                ("toggle", HOTKEY_ID_AUTO_CLICKER_START, "start_hotkey"),
            )
        else:
            clicker_entries = (
                ("start", HOTKEY_ID_AUTO_CLICKER_START, "start_hotkey"),
                ("stop", HOTKEY_ID_AUTO_CLICKER_STOP, "stop_hotkey"),
            )
        for action, hotkey_id, key_name in clicker_entries:
            text = str(clicker.get(key_name, "") or "")
            parsed = parse_hotkey_text(text)
            if not parsed:
                continue
            if not is_safe_global_hotkey(parsed):
                write_log(f"连点器热键 {text} 不适合全局启停，已跳过。",
                             category=LOG_APPLICATION,
                         )
                continue
            signature = hotkey_signature(parsed)
            action_name = {"start": "启动", "stop": "停止", "toggle": "启停"}[action]
            if signature in used_hotkeys:
                write_log(
                    f"连点器{action_name}热键 {text} 与其他热键冲突，已跳过。",
                             category=LOG_APPLICATION,
                         )
                continue
            if not self._register_hotkey(hwnd, hotkey_id, parsed):
                write_log(
                    f"连点器{action_name}热键 {text} 注册失败，将回退到轮询模式。",
                             category=LOG_APPLICATION,
                             critical=True,
                         )
                return False
            self.auto_clicker_ids[hotkey_id] = action
            used_hotkeys.add(signature)

        for binding_index, binding in enumerate(self._profile_bindings()):
            profile_name = str(binding.get("profile_name", "") or "")
            profile_id = str(binding.get("profile_id", "") or "")
            slot_id = str(binding.get("slot_id", "") or "")
            for action, base_id, key_name, target in (
                ("启动", HOTKEY_ID_PROFILE_START_BASE, "start_hotkey", self.profile_start_ids),
                ("停止", HOTKEY_ID_PROFILE_STOP_BASE, "stop_hotkey", self.profile_stop_ids),
            ):
                text = str(binding.get(key_name, "") or "")
                parsed = parse_hotkey_text(text)
                if not parsed:
                    continue
                if not is_safe_global_hotkey(parsed):
                    write_log(
                        f"方案“{profile_name}”{action}热键 {text} 不适合全局启停，已跳过。"
                    )
                    continue
                signature = hotkey_signature(parsed)
                if signature in used_hotkeys:
                    write_log(
                        f"方案“{profile_name}”{action}热键 {text} 与其他热键冲突，已跳过。"
                    )
                    continue
                hotkey_id = int(base_id) + binding_index
                if not self._register_hotkey(hwnd, hotkey_id, parsed):
                    write_log(
                        f"方案“{profile_name}”{action}热键 {text} 注册失败，将回退到轮询模式。"
                    )
                    return False
                target[hotkey_id] = (
                    (profile_id, slot_id) if action == "启动" else profile_id
                )
                used_hotkeys.add(signature)
        return True

    def register_global_hotkeys(self):
        try:
            self.unregister_global_hotkeys()
            hwnd = self.hotkey_hwnd()
            start_ok = self._register_hotkey(hwnd, HOTKEY_ID_START, self.start_parsed)
            stop_ok = self._register_hotkey(hwnd, HOTKEY_ID_STOP, self.stop_parsed)
            if start_ok and stop_ok:
                self.mapping_ids = {}
                self.auto_clicker_ids = {}
                self.profile_start_ids = {}
                self.profile_stop_ids = {}
                # 每次重新注册都重建失败清单，避免残留上一次的过期记录。
                self.mapping_registration_failures = {}
                used_hotkeys = {hotkey_signature(self.start_parsed), hotkey_signature(self.stop_parsed)}
                if not self._register_optional_hotkeys(hwnd, used_hotkeys):
                    self.unregister_global_hotkeys()
                    self._clear_registration_state()
                    return False
                for mapping in self.host.active_key_mappings():
                    sig = (mapping["modifiers"], mapping["vk"])
                    if sig in used_hotkeys:
                        write_log(f"按键映射{mapping['index'] + 1}热键 {mapping['hotkey']} 与启动/停止热键冲突，已跳过。",
                                     category=LOG_APPLICATION,
                                 )
                        continue
                    if not mapping.get("safe_global"):
                        if not getattr(self.host, "mapping_mode_chk", None) or not self.host.mapping_mode_chk.isChecked():
                            write_log(f"按键映射{mapping['index'] + 1}热键 {mapping['hotkey']} 是裸键，需要开启按键映射模式后才会生效。",
                                         category=LOG_APPLICATION,
                                     )
                        continue
                    ok = bool(user32.RegisterHotKey(hwnd, mapping["id"], mapping["modifiers"] | MOD_NOREPEAT, mapping["vk"]))
                    if ok:
                        self.mapping_ids[mapping["id"]] = mapping["index"]
                        used_hotkeys.add(sig)
                        self.mapping_registration_failures.pop(mapping["index"], None)
                    else:
                        # REVIEW-20260913-001 FR-006（返修轮 01）：注册失败必须**可见且可查**，
                        # 不能只留一条日志后仍被当作成功。
                        #
                        # 处置形态选择：本分支**不**让整个热键后端失败回退到轮询。理由：
                        # (1) ARCHITECTURE #90 只要求"工具/方案热键"注册失败时切换轮询，
                        #     未点名按键映射；本轮不实现尚未获维护者采纳的 ADV-006②
                        #     （是否为映射主动启用全局轮询兜底）。
                        # (2) 同一循环内既有的两个"该映射无法生效"分支（与启停热键冲突、
                        #     裸键未开启映射模式）都是记录日志后 `continue`，不让后端整体
                        #     失败；本分支与它们保持一致。
                        # (3) 因此后端继续使用 RegisterHotKey，其余已注册热键不受影响，
                        #     轮询定时器不被启动。
                        # 失败信息进入 `mapping_registration_failures` 供查询/诊断，
                        # 并在下次该映射注册成功时自动清除。
                        self.mapping_registration_failures[mapping["index"]] = str(
                            mapping.get("hotkey", "") or ""
                        )
                        write_log(
                            f"按键映射{mapping['index'] + 1}热键 {mapping['hotkey']} "
                            "注册失败，可能已被其他程序占用；本次运行期间该映射不生效，"
                            "请更换组合键或关闭占用该组合键的程序后重试。",
                                     category=LOG_APPLICATION,
                                     critical=True,
                                 )
                # FR-SCOPE009-002: 记录失败后立刻刷新界面提示位，否则用户看不到。
                self._refresh_mapping_failure_notice()
                self.registered = True
                return True
            write_log("注册全局热键失败，可能热键已被其他程序占用，回退到轮询模式。",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            self.unregister_global_hotkeys()
        except Exception as e:
            write_log(
                f"注册全局热键失败，回退到轮询模式: {safe_error_name(e)}",
                private_message=(
                    "注册全局热键失败，回退到轮询模式: "
                    f"{private_error_text(e)}"
                ),
                         category=LOG_APPLICATION,
                         critical=True,
                     )
        self._clear_registration_state()
        return False

    def refresh_hotkey_backend(self):
        if getattr(self.host, "_bulk_mapping_update", False):
            return
        if not getattr(self.host, "hotkey_timer", None):
            return
        if self.register_global_hotkeys():
            self.host.hotkey_timer.stop()
        else:
            self.host.hotkey_timer.start(self.host.HOTKEY_POLL_INTERVAL_MS)
        self.refresh_mapping_mode_hook()

    def update_hotkeys(self, _=None):
        try:
            start_parsed = self.validate_control_hotkey(self.host.hotkey_start_edit.text(), "F9", "启动热键")
            stop_parsed = self.validate_control_hotkey(self.host.hotkey_stop_edit.text(), "F10", "停止热键")
            if hotkey_signature(start_parsed) == hotkey_signature(stop_parsed):
                if not getattr(self.host, "is_switching_profile", False):
                    QMessageBox.warning(self.host, "热键冲突", "启动热键和停止热键不能相同，停止热键已恢复为 F10。")
                stop_parsed = parse_hotkey_text("F10")
                if hotkey_signature(start_parsed) == hotkey_signature(stop_parsed):
                    stop_parsed = parse_hotkey_text("F9")
            self.start_parsed = start_parsed
            self.stop_parsed = stop_parsed
            self.start_vk = start_parsed["vk"]
            self.stop_vk = stop_parsed["vk"]
            self.host.hotkey_start_edit.setText(start_parsed["display"])
            self.host.hotkey_stop_edit.setText(stop_parsed["display"])
            self.host.start_btn.setText(f"启动 ({start_parsed['display']})")
            self.host.stop_btn.setText(f"停止 ({stop_parsed['display']})")
            self.refresh_hotkey_backend()
        except Exception as e:
            write_log(
                f"更新热键失败: {safe_error_name(e)}",
                private_message=f"更新热键失败: {private_error_text(e)}",
                         category=LOG_APPLICATION,
                         critical=True,
                     )

    def check_hotkey(self):
        # L-37: 同一轮询周期内只执行第一个匹配的热键动作。这是轮询式热键检测的
        # 固有限制：当多个热键同时按下时，每个周期只分发一个动作以避免冲突操作
        # （如同时触发启动和停止）。优先级为脚本启停 > 连点器启停 >
        # 方案启停 > 按键映射。
        pressed_now = set()
        if hotkey_is_down(getattr(self, "start_parsed", None)):
            pressed_now.add("start")
        if hotkey_is_down(getattr(self, "stop_parsed", None)):
            pressed_now.add("stop")

        clicker = self._auto_clicker_bindings()
        clicker_tokens = {}
        if clicker.get("toggle"):
            parsed = parse_hotkey_text(clicker.get("start_hotkey", ""))
            if parsed and hotkey_is_down(parsed):
                pressed_now.add("clicker:toggle")
                clicker_tokens["toggle"] = "clicker:toggle"
        else:
            for action, key_name in (
                ("start", "start_hotkey"),
                ("stop", "stop_hotkey"),
            ):
                parsed = parse_hotkey_text(clicker.get(key_name, ""))
                token = f"clicker:{action}"
                if parsed and hotkey_is_down(parsed):
                    pressed_now.add(token)
                    clicker_tokens[action] = token

        profile_pressed = []
        for index, binding in enumerate(self._profile_bindings()):
            for action, key_name in (
                ("start", "start_hotkey"),
                ("stop", "stop_hotkey"),
            ):
                parsed = parse_hotkey_text(binding.get(key_name, ""))
                token = f"profile:{action}:{index}"
                if parsed and hotkey_is_down(parsed):
                    pressed_now.add(token)
                    profile_pressed.append((token, action, binding))

        mapping_pressed = set()
        mapping_to_execute = None
        input_operation_running = self._input_operation_is_running()
        # REVIEW-20260913-001 FR-006: 运行期也必须记录"当前按下"的映射键。此前整个
        # 检测块在运行期被跳过，mapping_poll_pressed 会被写成空集，于是运行结束后
        # 仍被按住的组合键满足边沿条件、补触发一次用户未意图的映射动作。这里只记录
        # 按下状态；运行期仍不执行映射（与注册路径的"消费但不执行"语义一致）。
        seen_mappings = set()
        for mapping in self.host.active_key_mappings():
            normalized = mapping.get("normalized")
            if not normalized or normalized in seen_mappings:
                continue
            seen_mappings.add(normalized)
            if not mapping.get("safe_global"):
                continue
            parsed = parse_hotkey_text(normalized)
            if hotkey_is_down(parsed):
                mapping_pressed.add(normalized)
                if (
                    not input_operation_running
                    and normalized not in self.mapping_poll_pressed
                    and mapping_to_execute is None
                ):
                    mapping_to_execute = mapping["index"]

        if "start" in pressed_now and "start" not in self.poll_pressed and not input_operation_running:
            self.host.start_task()
            self.poll_pressed = pressed_now
            self.mapping_poll_pressed = mapping_pressed
            return

        if (
            "stop" in pressed_now
            and "stop" not in self.poll_pressed
            and self._script_is_running()
        ):
            self.host.stop_task()
            self.poll_pressed = pressed_now
            self.mapping_poll_pressed = mapping_pressed
            return

        clicker_toggle = clicker_tokens.get("toggle")
        if clicker_toggle and clicker_toggle not in self.poll_pressed:
            # 轮询路径只响应按下沿（poll_pressed 边沿判定）；同键的启动/停止决策
            # 交给工具窗口，不能在此叠加输入所有权门槛（同注册路径注释）。
            self.host.toggle_auto_clicker_from_hotkey()
            self.poll_pressed = pressed_now
            self.mapping_poll_pressed = mapping_pressed
            return

        clicker_start = clicker_tokens.get("start")
        if (
            clicker_start
            and clicker_start not in self.poll_pressed
            and not input_operation_running
        ):
            self.host.start_auto_clicker_from_hotkey()
            self.poll_pressed = pressed_now
            self.mapping_poll_pressed = mapping_pressed
            return

        clicker_stop = clicker_tokens.get("stop")
        if clicker_stop and clicker_stop not in self.poll_pressed:
            checker = getattr(self.host, "auto_clicker_is_running", None)
            if checker and checker() is True:
                self.host.stop_auto_clicker()
                self.poll_pressed = pressed_now
                self.mapping_poll_pressed = mapping_pressed
                return

        for token, action, binding in profile_pressed:
            if token in self.poll_pressed:
                continue
            profile_id = str(binding.get("profile_id", "") or "")
            if action == "start" and not input_operation_running:
                self.host.profile_hotkey_controller.start_profile(
                    profile_id, str(binding.get("slot_id", "") or "")
                )
                self.poll_pressed = pressed_now
                self.mapping_poll_pressed = mapping_pressed
                return
            if action == "stop":
                self.host.profile_hotkey_controller.stop_profile(profile_id)
                self.poll_pressed = pressed_now
                self.mapping_poll_pressed = mapping_pressed
                return

        if mapping_to_execute is not None:
            self.host.execute_key_mapping(mapping_to_execute)
            self.poll_pressed = pressed_now
            self.mapping_poll_pressed = mapping_pressed
            return
        self.poll_pressed = pressed_now
        self.mapping_poll_pressed = mapping_pressed
