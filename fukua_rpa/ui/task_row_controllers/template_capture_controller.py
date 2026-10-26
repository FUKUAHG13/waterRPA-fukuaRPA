"""Screen-template capture coordination for a workflow step row."""

import contextlib
import os
import time
from functools import partial

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QMessageBox

from ...profile_paths import managed_path_reference
from ...task_model import config_bool
from ..lifecycle import schedule_widget_callback, widget_is_alive
from ..region_window_manager import region_window_manager


class _CaptureSignals(QObject):
    """Signals emitted by the background capture task."""

    finished = Signal(str)  # 成功时发出保存路径
    failed = Signal(str)  # 失败时发出错误信息


class _CaptureTask(QRunnable):
    """Runs ImageGrab.grab + PNG encode off the UI thread to avoid 4K freeze."""

    def __init__(self, bbox, save_path, signals):
        super().__init__()
        self.bbox = bbox
        self.save_path = save_path
        self.signals = signals

    def run(self):
        try:
            from PIL import ImageGrab

            screenshot = ImageGrab.grab(
                bbox=self.bbox,
                all_screens=True,
            )
            screenshot.save(self.save_path, "PNG")
            self.signals.finished.emit(self.save_path)
        except Exception as exc:
            # The save path was pre-reserved with O_EXCL; remove the empty
            # placeholder so a failed capture does not leave a 0-byte file.
            with contextlib.suppress(OSError):
                os.remove(self.save_path)
            self.signals.failed.emit(str(exc))


class TemplateCaptureController:
    def __init__(self, row):
        self.row = row
        # Hold a strong reference to the background capture's signal object for
        # the whole async lifetime; otherwise Python GC collects the QObject as
        # soon as save() returns and the finished/failed connections drop.
        self._active_signals = None

    def start(self):
        host = self.row
        if host.type_combo.currentText() not in (
            "左键单击",
            "左键双击",
            "右键单击",
            "鼠标悬停",
        ):
            return
        window = region_window_manager.get(host, "template")
        if window is not None:
            try:
                if window.isVisible():
                    window.raise_()
                    window.activateWindow()
                    return
            except RuntimeError:
                pass
        window = region_window_manager.open(
            host=host,
            key="template",
            purpose="template",
            on_selected=host.on_template_region_selected,
        )
        host.template_region_window = window
        window.setToolTip("框选要识别的屏幕目标；右键取消")
        window.destroyed.connect(partial(self.clear_window, window))

    def clear_window(self, expected_window=None, *_args):
        """Clear the region window reference, with identity check when provided."""
        if expected_window is not None and getattr(self.row, "template_region_window", None) is not expected_window:
            return
        self.row.template_region_window = None

    def on_region_selected(self, rect):
        host = self.row
        window = getattr(host, "template_region_window", None)
        if window is not None:
            with contextlib.suppress(RuntimeError):
                window.hide()
        physical_rect = tuple(int(value) for value in rect)
        schedule_widget_callback(
            host,
            120,
            lambda owner, selected=physical_rect: owner.save_template_capture(selected),
        )

    def save(self, rect):
        host = self.row
        try:
            x, y, width, height = (int(value) for value in rect)
            if width < 3 or height < 3:
                raise ValueError("框选区域太小，请重新框选稍大的目标。")

            main_window = host.window()
            base_dir = getattr(main_window, "base_dir", None)
            if not base_dir:
                from ...paths import get_base_dir

                base_dir = get_base_dir()
            template_dir = os.path.join(os.path.abspath(base_dir), "templates")
            os.makedirs(template_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            milliseconds = int(time.time() * 1000) % 1000
            path = os.path.join(
                template_dir,
                f"template_{stamp}_{milliseconds:03d}.png",
            )
            # Atomically claim a unique file name with O_CREAT|O_EXCL instead of
            # an exists-then-write probe, which raced with the background writer
            # (TOCTOU). EEXIST means the name is taken, so retry with a suffix.
            # The reserved handle is closed right away; the background thread
            # reopens the path to write the PNG. O_EXCL guarantees no other
            # capture picked the same name.
            reserved_fd = None
            suffix = 1
            for _ in range(1000):
                try:
                    reserved_fd = os.open(
                        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    )
                    break
                except FileExistsError:
                    path = os.path.join(
                        template_dir,
                        f"template_{stamp}_{milliseconds:03d}_{suffix}.png",
                    )
                    suffix += 1
            else:
                path = os.path.join(
                    template_dir,
                    f"template_{stamp}_{milliseconds:03d}_{time.time_ns()}.png",
                )
                reserved_fd = os.open(
                    path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            os.close(reserved_fd)

            # ImageGrab.grab + PNG encode runs in a background thread to avoid
            # freezing the UI (50-100 ms on 4K displays).
            host.setToolTip("正在截图...")
            signals = _CaptureSignals()
            self._active_signals = signals
            signals.finished.connect(
                lambda saved_path: self._on_capture_done(saved_path, main_window, base_dir)
            )
            signals.failed.connect(self._on_capture_failed)
            task = _CaptureTask((x, y, x + width, y + height), path, signals)
            QThreadPool.globalInstance().start(task)
        except Exception as error:
            QMessageBox.warning(host, "模板保存失败", str(error))

    def _on_capture_done(self, path, main_window, base_dir):
        """Post-capture validation and UI update (runs on UI thread via signal)."""
        self._active_signals = None
        host = self.row
        if not widget_is_alive(host):
            return
        try:
            host.setToolTip("")
            from ...vision.core import template_detail_status

            use_gray = True
            if config_bool(host.custom_data.get("custom_en", False)):
                use_gray = config_bool(host.custom_data.get("custom_gray", True))
            elif widget_is_alive(main_window) and hasattr(main_window, "gray_chk"):
                use_gray = bool(main_window.gray_chk.isChecked())
            valid, reason = template_detail_status(path, use_gray)
            if not valid:
                with contextlib.suppress(OSError):
                    os.remove(path)
                raise ValueError(reason)

            reference = managed_path_reference(path, base_dir)
            host.commit_external_edit(
                "快捷截图模板",
                lambda: host.value_input.setText(reference),
            )
        except Exception as error:
            QMessageBox.warning(host, "模板保存失败", str(error))

    def _on_capture_failed(self, message):
        """Handle background capture failure (runs on UI thread via signal)."""
        self._active_signals = None
        host = self.row
        if not widget_is_alive(host):
            return
        host.setToolTip("")
        QMessageBox.warning(host, "模板保存失败", message)
