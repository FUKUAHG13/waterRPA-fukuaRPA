# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Orderly main-window shutdown and final state persistence."""

from __future__ import annotations

import contextlib
import os
import weakref
from typing import TYPE_CHECKING

from ...constants import APP_VERSION, PRODUCT_NAME
from ...log_policy import (
    LOG_RUN,
)
from ...logging_service import flush_logs, write_log
from ...privacy import private_error_text, safe_error_name
from ..lifecycle import safe_close, schedule_widget_callback

if TYPE_CHECKING:
    from .host_view import HostView


class ShutdownController:
    """Coordinate asynchronous workers and UI resources during shutdown."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self._close_retry_count = 0
        self._hook_stop_retry_count = 0

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已销毁")
        return host

    def close_event(self, event):
        host = self.host
        # 停止计划任务定时器，防止退出过程中触发新运行
        if hasattr(host, 'scheduled_service') and host.scheduled_service is not None:
            host.scheduled_service.shutdown()
        try:
            integrity_worker = getattr(host, "integrity_worker", None)
            if integrity_worker and integrity_worker.isRunning():
                integrity_worker.requestInterruption()
                if not integrity_worker.wait(1500):
                    self._close_retry_count += 1
                    if self._close_retry_count > 10:
                        write_log(
                            "完整性线程未能在限定时间内停止，放弃等待并继续退出",
                            private_message=(
                                "完整性线程未能在限定时间内停止，"
                                f"已重试 {self._close_retry_count - 1} 次，放弃等待并继续退出"
                            ),
                                     category=LOG_RUN,
                                     critical=True,
                                 )
                    else:
                        event.ignore()
                        schedule_widget_callback(
                            host, 100, lambda owner: owner.close()
                        )
                        return
            else:
                self._close_retry_count = 0
            # FR-20260812-D10-a: 诊断导出线程（report_worker）同样必须等待——
            # 它不挂父窗口（有意设计，避免冻结关闭），但退出时不等待会造成
            # "QThread destroyed while running"。requestInterruption + 有界等待，
            # 超时放弃并记录（诊断线程无输入副作用，可安全放弃）。
            report_worker = getattr(host, "report_worker", None)
            if report_worker and report_worker.isRunning():
                report_worker.requestInterruption()
                if not report_worker.wait(1500):
                    write_log(
                        "诊断导出线程未能在限定时间内停止，继续退出",
                        private_message=(
                            "诊断导出线程未能在限定时间内停止，继续退出"
                        ),
                                 category=LOG_RUN,
                                 critical=True,
                             )
            if not host._close_state_saved:
                autosave_timer = getattr(host, "profiles_autosave_timer", None)
                if autosave_timer:
                    autosave_timer.stop()
                if not host.stop_key_mapping_hook():
                    self._hook_stop_retry_count += 1
                    if self._hook_stop_retry_count > 10:
                        write_log(
                            "按键映射钩子未能在限定时间内停止，放弃等待并继续退出",
                            private_message=(
                                "按键映射钩子未能在限定时间内停止，"
                                f"已重试 {self._hook_stop_retry_count - 1} 次，"
                                "放弃等待并继续退出"
                            ),
                                     category=LOG_RUN,
                                     critical=True,
                                 )
                    else:
                        event.ignore()
                        schedule_widget_callback(
                            host, 100, lambda owner: owner.close()
                        )
                        return
                else:
                    self._hook_stop_retry_count = 0
                host.hotkey_controller.unregister_global_hotkeys()
                host.profile_hotkey_controller.shutdown()
                host.settings.setValue(
                    "window_geometry", host.saveGeometry()
                )
                settings_dialog = getattr(host, "settings_dialog", None)
                if settings_dialog:
                    settings_dialog.save_dialog_geometry()
                host._shutdown_save_ok = host.persist_profiles_state(
                    force=True
                )
                host.settings.sync()
                host._close_state_saved = True

            recorder = getattr(host, "recorder_ui", None)
            safe_close(recorder)
            region_window = getattr(host, "region_win", None)
            safe_close(region_window)
            settings_dialog = getattr(host, "settings_dialog", None)
            safe_close(settings_dialog)
            safe_close(getattr(host, "_auto_clicker_window", None))
            host.close_all_task_config_dialogs()
            for picker in list(
                getattr(host, "mapping_pickers", {}).values()
            ):
                safe_close(picker)
            for picker in list(getattr(host, "dodge_pickers", {}).values()):
                safe_close(picker)
            for inspector in list(
                getattr(host, "mapping_inspector_dialogs", set())
            ):
                safe_close(inspector)

            worker = getattr(host, "worker", None)
            if worker and worker.isRunning():
                host._close_after_worker = True
                # 2026-10-21 裁定 ②：从**首次**请求停止起 15 秒后提供强制退出入口；
                # 重复关窗不得重置期限（用标记位守护）。
                if not getattr(host, "_shutdown_watchdog_started", False):
                    host._shutdown_watchdog_started = True
                    _arm_force_exit_prompt(host)
                host.run_controller.stop()
                host.centralWidget().setEnabled(False)
                host.setWindowTitle(
                    f"{PRODUCT_NAME} {APP_VERSION} - 正在安全停止脚本…"
                )
                event.ignore()
                return
            host.feedback_controller.close_all()
            if host.mapping_backend is not None:
                host.mapping_backend.close()
        except Exception as error:
            # 保存/清理异常时不得放行关闭：worker 可能仍在运行，直接 accept 会让
            # 脚本继续跑而窗口消失（R15 审查①的 fail-open）。此时改为 ignore，
            # 由用户重试或等待 worker 结束。
            running = getattr(host, "worker", None)
            if running is not None and running.isRunning():
                write_log(
                    f"退出前保存异常（{safe_error_name(error)}），"
                    "且脚本仍在运行：已阻止关闭并请求停止",
                    category=LOG_RUN,
                    critical=True,
                )
                # 只 ignore 而不停止，会让用户反复点关闭却毫无反应（R16 ①）。
                host.run_controller.stop()
                event.ignore()
                return
            write_log(
                f"退出前保存异常: {safe_error_name(error)}",
                private_message=(
                    f"退出前保存异常: {private_error_text(error)}"
                ),
                         category=LOG_RUN,
                         critical=True,
                     )
        if getattr(host, "_shutdown_save_ok", True):
            try:
                host.session_guard.close()
            except Exception as error:
                write_log(
                    f"清理会话标记失败: {safe_error_name(error)}",
                    private_message=(
                        f"清理会话标记失败: {private_error_text(error)}"
                    ),
                             category=LOG_RUN,
                             critical=True,
                         )
        scheduled_service = getattr(host, "scheduled_service", None)
        if scheduled_service is not None:
            scheduled_service.finalize_logging()
        event.accept()


#: 停止 worker 后等待多久提供「强制退出」入口（裁定 ②）。
FORCE_EXIT_PROMPT_MS = 15_000


def _arm_force_exit_prompt(host) -> None:
    """Arm the 15s force-exit prompt bound to *host*'s lifetime.

    DESIGN: 定时器必须挂在宿主上并随宿主销毁取消。裸 ``QTimer.singleShot`` 在
    宿主销毁后仍会到期，随后在无关调用栈里构造真实模态框并 ``exec()``——
    测试套件会因此永久阻塞，生产也可能弹出无主对话框（FR-20261021-001）。
    非 Qt 宿主（测试替身）没有可绑定的生命周期，不武装。
    """
    from PySide6.QtCore import QObject

    if not isinstance(host, QObject):
        return
    schedule_widget_callback(host, FORCE_EXIT_PROMPT_MS, prompt_force_exit)


def prompt_force_exit(host) -> None:
    """Offer 「继续等待 / 强制退出」 after the worker fails to stop in time.

    DESIGN: 提示必须是**顶层**对话框，不能挂在已禁用的主界面上（裁定 ②）。
    默认按钮是「继续等待」，不自动强退；等待期间 worker 结束则静默返回，
    由正常退出路径收尾。
    """
    worker = getattr(host, "worker", None)
    if worker is None or not worker.isRunning():
        return
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox()
    box.setWindowTitle("脚本仍未停止")
    box.setIcon(QMessageBox.Warning)
    box.setText("脚本在 15 秒内没有停止。")
    box.setInformativeText(
        "可以继续等待，或强制退出。\n\n"
        "强制退出可能残留按键状态，也可能丢失未保存的内容。"
    )
    wait_btn = box.addButton("继续等待", QMessageBox.AcceptRole)
    force_btn = box.addButton("强制退出", QMessageBox.DestructiveRole)
    box.setDefaultButton(wait_btn)
    box.exec()
    if box.clickedButton() is force_btn:
        # 提示期间 worker 可能已结束：此时应走正常退出，不能强杀（R16 复验）。
        if worker.isRunning():
            force_exit(host)
        return
    if worker.isRunning():
        _arm_force_exit_prompt(host)


def force_exit(host) -> None:
    """Release tracked inputs with a bound, then terminate this process.

    SAFETY: 只释放本程序记录过的键鼠状态，不发无差别的 key-up；清理本身有界，
    失败也继续终止，绝不无限等待（裁定 ②）。
    """
    write_log(
        "用户确认强制退出：可能有残留按键状态与未保存内容（非正常完成）",
        category=LOG_RUN,
        critical=True,
    )
    # 先请求停止，避免释放后 worker 继续注入（R17 审查 ③）。
    with contextlib.suppress(Exception):
        host.run_controller.stop()
    engine = getattr(getattr(host, "run_controller", None), "engine", None)
    if engine is not None:
        # 释放必须**有界**：worker 可能仍持着输入锁，直接调用会卡死在这里
        # （R16 复验）。放到守护线程里等 2 秒，超时就继续终止。
        import threading

        def _release():
            try:
                engine.release_injected_inputs()
            except Exception as error:  # noqa: BLE001 - 清理失败也要继续终止
                write_log(
                    f"强制退出时释放输入失败: {safe_error_name(error)}",
                    category=LOG_RUN,
                    critical=True,
                )

        releaser = threading.Thread(target=_release, name="force-exit-release", daemon=True)
        releaser.start()
        releaser.join(2.0)
        if releaser.is_alive():
            write_log(
                "强制退出：释放输入超时（可能残留按键状态）",
                category=LOG_RUN,
                critical=True,
            )
    try:
        import psutil

        me = psutil.Process()
        for child in me.children(recursive=True):
            # 只杀本程序的执行子进程：18.0「启动程序」打开的**用户程序**不是我们的
            # 子任务，误杀会关掉用户正在用的软件（R17 审查 ②）。
            try:
                cmdline = " ".join(child.cmdline()).lower()
            except Exception:  # noqa: BLE001 - 取不到命令行就跳过
                continue
            if "fukuarpa" not in cmdline and "run_unittest" not in cmdline:
                continue
            try:
                child.kill()
            except Exception:  # noqa: BLE001 - 子进程可能已退出
                continue
    except Exception:  # noqa: BLE001 - psutil 不可用时不阻塞退出
        pass
    # 日志是队列写入：不 flush 就 os._exit 会把「强制终止」这条记录丢掉（R17 审查 ①）。
    # 尽力而为：flush 只能说明「通道看起来正常」，不是这条记录的写入回执；
    # 通道关闭或磁盘满/权限拒绝时仍可能丢失。返回 False 时再直写一行兜底。
    flushed = False
    with contextlib.suppress(Exception):
        flushed = flush_logs(timeout=2.0)
    if not flushed:
        # 直写也必须**有界**：worker 可能正持着日志写锁，直接调用会无界阻塞，
        # 让 os._exit 永远执行不到（R17 复验）。放进守护线程等 0.5 秒即走。
        import threading

        def _direct():
            with contextlib.suppress(Exception):
                from ...logging_service import _append_direct

                _append_direct("[强制退出] 日志队列未能及时落盘，本条为直写兜底。")

        writer = threading.Thread(target=_direct, name="force-exit-log", daemon=True)
        writer.start()
        writer.join(0.5)
    os._exit(1)
