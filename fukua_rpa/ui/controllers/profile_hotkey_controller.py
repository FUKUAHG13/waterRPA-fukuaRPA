# pyright: reportAttributeAccessIssue=false
"""Global profile hotkey slots backed by one main-window run coordinator."""

from __future__ import annotations

import contextlib
import uuid
import weakref
from typing import TYPE_CHECKING

from ...log_policy import (
    LOG_APPLICATION,
)
from ...logging_service import write_log
from ...profile_hotkeys import (
    normalize_profile_hotkey_slots,
    serialize_profile_hotkey_slots,
)
from ...win32_api import is_safe_global_hotkey, parse_hotkey_text
from ..lifecycle import is_runtime_editing_locked

if TYPE_CHECKING:
    from .host_view import HostView


_ENABLED_KEY = "profile_hotkeys/enabled"
_SLOTS_KEY = "profile_hotkeys/slots_json"


class ProfileHotkeyController:
    """Own multi-profile launch bindings without opening another process."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self._dialog = None
        self.active_profile_id = ""
        self._slots = normalize_profile_hotkey_slots(
            host.settings.value(_SLOTS_KEY, "")
        )

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("多方案快捷键所属窗口已销毁")
        return host

    def enabled(self) -> bool:
        return bool(self.host.settings.value(_ENABLED_KEY, False, type=bool))

    def set_enabled(self, enabled: bool) -> None:
        host = self.host
        host.settings.setValue(_ENABLED_KEY, bool(enabled))
        host.settings.sync()
        self.refresh_status()
        host.hotkey_controller.refresh_hotkey_backend()
        host.append_application_log(
            "多方案快捷启动已" + ("开启" if enabled else "关闭")
        )

    def slots(self) -> list[dict]:
        return [dict(item) for item in self._slots]

    def save_slots(self, slots: object) -> None:
        host = self.host
        self._slots = normalize_profile_hotkey_slots(slots)
        host.settings.setValue(
            _SLOTS_KEY, serialize_profile_hotkey_slots(self._slots)
        )
        host.settings.sync()
        self.refresh_status()
        host.hotkey_controller.refresh_hotkey_backend()
        host.append_application_log(
            f"多方案快捷启动已保存 {len(self._slots)} 个槽位"
        )

    def configured_bindings(self) -> list[dict]:
        """Return enabled slots whose stable profile still exists."""

        model = self.host.profile_model
        result = []
        for index, slot in enumerate(self._slots):
            if not slot.get("enabled"):
                continue
            profile_id = str(slot.get("profile_id", ""))
            profile_name = model.find_by_id(profile_id)
            if not profile_name:
                continue
            start = parse_hotkey_text(slot.get("start_hotkey", ""))
            stop_text = str(slot.get("stop_hotkey", "") or "")
            stop = parse_hotkey_text(stop_text) if stop_text else None
            if not is_safe_global_hotkey(start) or (
                stop_text and not is_safe_global_hotkey(stop)
            ):
                continue
            result.append(
                {
                    **slot,
                    "index": index,
                    "profile_name": profile_name,
                }
            )
        return result

    def active_bindings(self) -> list[dict]:
        """Return configured bindings only while the feature is enabled."""

        return self.configured_bindings() if self.enabled() else []

    def refresh_status(self) -> None:
        host = self._host_ref()
        if host is None:
            return
        checkbox = getattr(host, "profile_hotkey_mode_chk", None)
        if checkbox is not None and checkbox.isChecked() != self.enabled():
            checkbox.blockSignals(True)
            checkbox.setChecked(self.enabled())
            checkbox.blockSignals(False)
        label = getattr(host, "profile_hotkey_status_label", None)
        if label is not None:
            total = len(self._slots)
            if not total:
                text = "尚未配置方案快捷键"
            elif not self.enabled():
                text = f"已配置 {total} 个槽位（功能未开启）"
            else:
                text = (
                    f"已配置 {total} 个槽位，"
                    f"可用 {len(self.active_bindings())} 个"
                )
            label.setText(text)

    def on_profiles_changed(self) -> None:
        """Refresh bindings after a profile is created, renamed or deleted."""

        self.refresh_status()
        dialog = self._dialog
        if dialog is not None:
            try:
                dialog.refresh_profiles()
            except RuntimeError:
                self._dialog = None
        self.host.hotkey_controller.refresh_hotkey_backend()

    def show_dialog(self) -> None:
        from ..profile_hotkey_dialog import ProfileHotkeyDialog

        previous = self._dialog
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                previous.close()
        dialog = ProfileHotkeyDialog(
            self.host,
            self.slots(),
        )
        self._dialog = dialog
        dialog.saved.connect(self.save_slots)
        dialog.destroyed.connect(
            lambda *_args, current=dialog: setattr(
                self,
                "_dialog",
                None if self._dialog is current else self._dialog,
            )
        )
        dialog.show()
        # FR-20261022-002: 方案快捷键对话框是 parent=None 的顶层非模态窗口，不在
        # 设置对话框子树内；运行期新建时必须立即只读，停止后由执行控制器恢复。
        if is_runtime_editing_locked(self.host):
            dialog.setEnabled(False)
        dialog.raise_()
        dialog.activateWindow()

    def start_profile(self, profile_id: str, slot_id: str = "") -> bool:
        host = self.host
        profile_id = str(profile_id or "")
        if not self.enabled() or not host.runtime_ready:
            return False
        if host.run_controller.is_active or host.auto_clicker_is_running():
            host.show_start_tooltip("已有脚本或连点器正在运行")
            return False
        binding = next(
            (
                item
                for item in self.active_bindings()
                if item.get("profile_id") == profile_id
                and (
                    not slot_id
                    or str(item.get("slot_id", "")) == str(slot_id)
                )
            ),
            None,
        )
        if binding is None:
            return False
        model = host.profile_model
        name = model.find_by_id(profile_id)
        cfg = model.get_config_by_id(profile_id)
        if not name or not cfg:
            write_log("多方案快捷启动已忽略：绑定方案不存在",
                         category=LOG_APPLICATION,
                         critical=True,
                     )
            self.refresh_status()
            return False
        tasks = cfg.get("tasks", [])
        error = host.workflow_review_controller.validate_tasks(tasks, cfg)
        if error:
            host.show_critical_message(f"方案“{name}”无法启动", error)
            return False
        # 风险确认必须记在**目标方案**名下：本次运行用的是该方案的独立快照，
        # 全程不切换 current_name，若按当前编辑方案去重会互相污染。
        if not host.workflow_review_controller.confirm_loop_risks(
            tasks, cfg, profile_id=profile_id
        ):
            return False
        started = host.execution_controller.prepare_and_launch_profile(
            profile_id,
            "profile_hotkey",
            str(slot_id or ""),
            uuid.uuid4().hex,
            None,
        )
        if started:
            self.active_profile_id = profile_id
        return bool(started)

    def stop_profile(self, profile_id: str) -> bool:
        if (
            not self.host.run_controller.is_active
            or str(profile_id or "") != self.active_profile_id
        ):
            return False
        self.host.stop_task()
        return True

    def on_run_finished(self) -> None:
        self.active_profile_id = ""

    def shutdown(self) -> None:
        dialog = self._dialog
        self._dialog = None
        if dialog is not None:
            with contextlib.suppress(RuntimeError):
                dialog.close()
