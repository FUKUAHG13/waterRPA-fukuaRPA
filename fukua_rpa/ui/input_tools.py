# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false
"""Screen region picking, coordinate picking, hotkey capture, and operation recording."""

import contextlib
import ctypes
import logging
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..coordinates import (
    ScreenCoordinateMapper,
    _ensure_physical_dpi_awareness,
    input_to_physical_coordinates,
    input_to_physical_rect,
    physical_to_input_coordinates,
    physical_to_input_rect,
)
from ..hook_backend import win32_hook_backend
from ..log_policy import (
    LOG_ACTION,
)
from ..logging_service import write_log
from .lifecycle import schedule_close, schedule_widget_callback
from .theme import (
    COLOR_DANGER,
    COLOR_FOCUS,
    COLOR_PRIMARY,
    INPUT_PANEL_ACCENT,
    INPUT_PANEL_BG,
    INPUT_PANEL_BODY,
    INPUT_PANEL_BORDER,
    INPUT_PANEL_MUTED,
    INPUT_PANEL_SHADOW,
    INPUT_PANEL_TITLE,
    OVERLAY_BG,
    OVERLAY_BG_DANGER,
)

_logger = logging.getLogger(__name__)

# Minimum pixel displacement to classify a press+release as a drag rather than
# a click during operation recording.
_DRAG_THRESHOLD_PX = 10


def _ui_scale() -> float:
    """Current UI zoom factor (1.0 == 100%); reads the app property set by theme.py.

    Falls back to 1.0 when no ``QApplication`` is available so the helpers stay
    safe to call from any context (review item #10).
    """

    try:
        app = QApplication.instance()
        if app is not None:
            value = app.property("_fukua_ui_scale")
            if value:
                return max(0.5, min(3.0, float(value)))
    except Exception as exc:
        _logger.debug("解析缩放值失败: %s", exc, exc_info=True)
        pass
    return 1.0


def _scaled_arial(size, *, bold=False, weight=QFont.Normal):
    """Build an Arial font whose point size tracks the UI zoom factor."""

    return QFont(
        "Arial",
        max(1, int(round(size * _ui_scale()))),
        QFont.Bold if bold else weight,
    )
from ..pyautogui_runtime import pyautogui  # noqa: E402
from ..recording_model import (  # noqa: E402
    MAX_RECORDED_EVENTS,
    build_recording_tasks,
)
from ..win32_api import (  # noqa: E402
    HOOKPROC,
    KBDLLHOOKSTRUCT,
    MODIFIER_VKS,
    MSLLHOOKSTRUCT,
    PM_NOREMOVE,
    VK_ESCAPE,
    VK_F8,
    VK_MAP,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_QUIT,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    hotkey_text_from_pressed_vks,
    kernel32,
    key_event_to_hotkey_text,
    parse_hotkey_text,
    pressed_hotkey_display_text,
    user32,
)

gdi32 = ctypes.windll.gdi32


def _configure_gdi_capture_signatures():
    """Declare pointer-sized GDI signatures used by the full-desktop capture."""

    try:
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
    except Exception as exc:  # noqa: BLE001 - capture has an explicit Qt fallback
        _logger.warning("GDI 截图 API 签名配置失败，将在截图失败时回退 Qt: %s", exc)


_configure_gdi_capture_signatures()


class RegionWindow(QWidget):
    region_selected = Signal(tuple)
    regions_selected = Signal(list)

    PURPOSE_COPY = {
        "recognition": (
            "选取识别范围",
            "按住左键拖动，框出允许识别的屏幕区域",
        ),
        "template": (
            "快捷选图",
            "按住左键拖动，完整框住要识别的目标，松开后自动保存模板",
        ),
        "capture": (
            "选取截图范围",
            "按住左键拖动，框出本步骤需要保存的画面范围",
        ),
        "condition": (
            "选取条件范围",
            "按住左键拖动，框出条件只需要检查的屏幕区域",
        ),
    }

    def __init__(self, multi=False, purpose="recognition", frozen_snapshot=None, restrict_rect=None):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.multi = multi
        self.purpose = purpose if purpose in self.PURPOSE_COPY else "recognition"

        self.screen_mapper = ScreenCoordinateMapper()

        # DESIGN: 窗口几何使用 PMv2 物理像素坐标（非 Qt 逻辑坐标），与 GDI BitBlt
        # 截图在同一坐标空间。这确保混合 DPI 多屏下窗口完整覆盖虚拟桌面，
        # 避免 System-DPI 逻辑坐标在 PMv2 上下文中被错误解释导致的黑块。
        # 单屏或各屏 DPI 一致时物理=逻辑，行为不变。
        # INVARIANT: _physical_origin 为 (vx, vy) 时表示本地控件坐标与物理像素
        # 1:1 对应（local + origin = physical）；为 None 时回退到 screen_mapper
        # 逻辑坐标路径（旧行为）。
        self._physical_origin = None
        if _ensure_physical_dpi_awareness():
            vx, vy, vw, vh = self._virtual_desktop_bounds()
            if vw > 0 and vh > 0:
                self._physical_origin = (vx, vy)
                self.setGeometry(vx, vy, vw, vh)
            else:
                self.screen_mapper.apply_to(self)
        else:
            self.screen_mapper.apply_to(self)

        # DESIGN: restrict_rect 是引擎输入坐标 (x, y, w, h)。提供时把框选限制在该矩形内，
        # 用于”直到条件”子区域选择，确保子区域落在大区域范围内。换算成本地控件坐标
        # 存储；未提供时为 None，框选行为与原来完全一致（向后兼容）。
        self._restrict_local = self._physical_restrict_to_local(restrict_rect)

        # DESIGN: 调用方可在触发框图的瞬间预截取快照并传入，避免事件队列
        # 延迟导致截取到变动后的画面。未传入时回退到即时截图（原有行为）。
        if frozen_snapshot is not None and not frozen_snapshot.isNull():
            self._background = frozen_snapshot
        else:
            self._background = self._grab_virtual_desktop()

        self.start_point = None
        self.end_point = None
        self.current_pos = QPoint(0, 0)
        self.selection_rect = QRect()
        self.selections = []
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _physical_restrict_to_local(self, restrict_rect):
        """Convert an input-space restrict rect into a local widget QRect."""

        if not restrict_rect:
            return None
        try:
            x, y, w, h = (int(value) for value in restrict_rect)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        if self._physical_origin is not None:
            physical = input_to_physical_rect((x, y, w, h))
            if physical is None:
                return None
            x, y, w, h = physical
            ox, oy = self._physical_origin
            return QRect(x - ox, y - oy, w, h)
        first = input_to_physical_coordinates(x, y)
        last = input_to_physical_coordinates(x + w, y + h)
        top_left = self.screen_mapper.physical_to_local(*first)
        bottom_right = self.screen_mapper.physical_to_local(*last)
        return QRect(top_left, bottom_right).normalized()

    def _clamped_to_restrict(self, point):
        """Clamp a local point into the restrict rect (no-op when unrestricted)."""

        restrict = self._restrict_local
        if restrict is None or not restrict.isValid():
            return point
        return QPoint(
            max(restrict.left(), min(point.x(), restrict.right())),
            max(restrict.top(), min(point.y(), restrict.bottom())),
        )

    def _grab_virtual_desktop(self):
        """Capture a static snapshot of the entire virtual desktop.

        Returns a QPixmap covering the full virtual screen, or None on
        failure so that paintEvent falls back to the translucent overlay.

        DESIGN: 使用 Windows GDI（GetDC + BitBlt）截取整个虚拟桌面为单一
        物理像素位图，而非 Qt 的逐屏 grabWindow 拼接。混合 DPI 环境中各屏
        devicePixelRatio 不同，Qt grabWindow 返回的 pixmap 带有各自 DPR，
        拼接到统一画布时尺寸/偏移计算错误导致黑色缺口。GDI 截图天然产出
        统一物理像素坐标系下的单张位图，彻底绕过 per-screen DPR 问题。

        INVARIANT: 虚拟桌面范围通过 EnumDisplayMonitors 在 Per-Monitor V2
        上下文下枚举获得，与 coordinates.py 的 _physical_monitors 一致，
        避免 GetSystemMetrics(76-79) 在 DPI 虚拟化下返回逻辑坐标导致截图
        区域与实际 DC 范围不匹配。BitBlt 使用 SRCCOPY | CAPTUREBLT 确保
        DWM 合成层/分层窗口内容被捕获。
        """
        try:
            pixmap = self._grab_virtual_desktop_gdi()
            if pixmap is not None and not pixmap.isNull():
                return pixmap
            # GDI 路径失败或产出全黑位图，回退到 Qt grabWindow 兜底。
            _logger.warning("GDI 截图不可用，回退到 Qt grabWindow")
            return self._grab_virtual_desktop_qt()
        except Exception as exc:
            _logger.warning("虚拟桌面截图失败: %s", exc, exc_info=True)
            return None

    def _grab_virtual_desktop_gdi(self):
        """GDI BitBlt capture of the full virtual desktop (primary path)."""
        # 通过 EnumDisplayMonitors 获取真实物理像素范围，避免
        # GetSystemMetrics(76-79) 受 DPI 虚拟化影响返回逻辑坐标。
        vx, vy, vw, vh = self._virtual_desktop_bounds()
        if vw < 1 or vh < 1:
            _logger.warning("虚拟桌面范围无效: %dx%d at (%d,%d)", vw, vh, vx, vy)
            return None

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            _logger.warning("GetDC(None) 失败")
            return None
        hdc_mem = None
        hbitmap = None
        old_obj = None
        try:
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                _logger.warning("CreateCompatibleDC 失败")
                return None

            # BITMAPINFOHEADER for a top-down 32-bit BGRA DIB section.
            class _BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = vw
            bmi.biHeight = -vh  # negative = top-down row order
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB

            ppv_bits = ctypes.c_void_p()
            hbitmap = gdi32.CreateDIBSection(
                hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(ppv_bits), None, 0
            )
            if not hbitmap or not ppv_bits.value:
                _logger.warning("CreateDIBSection 失败")
                return None

            old_obj = gdi32.SelectObject(hdc_mem, hbitmap)
            # SRCCOPY | CAPTUREBLT: CAPTUREBLT (0x40000000) 确保 DWM 合成层
            # 和分层窗口（如半透明覆盖、OSD）的内容被捕获到截图中。
            rop = 0x00CC0020 | 0x40000000
            result = gdi32.BitBlt(hdc_mem, 0, 0, vw, vh, hdc_screen, vx, vy, rop)
            if not result:
                _logger.warning("BitBlt 失败")
                return None

            # Copy pixel data into a QImage (Format_RGB32 matches BGRA layout
            # on little-endian Windows; .copy() detaches from the DIB buffer).
            stride = vw * 4
            image = QImage(
                bytes((ctypes.c_ubyte * (stride * vh)).from_address(ppv_bits.value)),
                vw,
                vh,
                stride,
                QImage.Format_RGB32,
            ).copy()

            # 诊断：采样左上、中心、右下 3 个像素，若全黑则视为截图失败。
            if self._image_is_all_black(image, vw, vh):
                _logger.warning(
                    "GDI 截图全黑 (%dx%d)，可能 DC 范围与坐标不匹配", vw, vh
                )
                return None

            return QPixmap.fromImage(image)
        finally:
            if old_obj is not None and hdc_mem:
                gdi32.SelectObject(hdc_mem, old_obj)
            if hbitmap:
                gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)

    @staticmethod
    def _virtual_desktop_bounds():
        """Return (x, y, width, height) of the virtual desktop in physical pixels.

        使用 EnumDisplayMonitors 在 Per-Monitor V2 上下文下枚举所有监视器，
        计算包围盒。与 coordinates.py 的 _physical_monitors 使用相同的 DPI
        感知切换，确保返回真实物理像素坐标而非 DPI 虚拟化坐标。
        """
        from ..win32_api import MONITORENUMPROC, MONITORINFOEXW

        if not _ensure_physical_dpi_awareness():
            # PMv2 不可用（DPI 上下文已被 Qt 锁定等）：回退到 GetSystemMetrics
            # 获取当前 DPI 上下文下的虚拟桌面范围，避免 EnumDisplayMonitors
            # 在虚拟化上下文中返回与截图 DC 不一致的矩形。
            SM_XVIRTUALSCREEN = 76
            SM_YVIRTUALSCREEN = 77
            SM_CXVIRTUALSCREEN = 78
            SM_CYVIRTUALSCREEN = 79
            try:
                vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
                vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
                vw = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
                vh = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
                return (vx, vy, vw, vh)
            except Exception as exc:
                _logger.warning("GetSystemMetrics 虚拟桌面范围失败: %s", exc)
                return (0, 0, 0, 0)

        bounds = []

        def enum_proc(hmonitor, _hdc, _rectangle, _data):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                rect = info.rcMonitor
                bounds.append(
                    (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                )
            return True

        try:
            callback = MONITORENUMPROC(enum_proc)
            user32.EnumDisplayMonitors(None, None, callback, 0)
        except Exception as exc:
            _logger.warning("EnumDisplayMonitors 失败: %s", exc)
            return (0, 0, 0, 0)

        if not bounds:
            return (0, 0, 0, 0)

        min_x = min(b[0] for b in bounds)
        min_y = min(b[1] for b in bounds)
        max_x = max(b[2] for b in bounds)
        max_y = max(b[3] for b in bounds)
        return (min_x, min_y, max_x - min_x, max_y - min_y)

    @staticmethod
    def _image_is_all_black(image, width, height):
        """Sample 3 pixels (top-left, center, bottom-right); True if all black."""
        if width < 1 or height < 1:
            return True
        points = [
            (0, 0),
            (width // 2, height // 2),
            (width - 1, height - 1),
        ]
        for px, py in points:
            pixel = image.pixel(px, py)
            # pixel is 0xAARRGGBB; mask out alpha and check RGB
            if pixel & 0x00FFFFFF != 0:
                return False
        return True

    @staticmethod
    def _grab_virtual_desktop_qt():
        """Fallback: Qt screen grab (may have DPR issues but better than black)."""
        try:
            screen = QApplication.primaryScreen()
            if screen is None:
                return None
            # grabWindow(0) captures the entire virtual desktop via Qt.
            pixmap = screen.grabWindow(0)
            if pixmap.isNull():
                return None
            return pixmap
        except Exception as exc:
            _logger.warning("Qt grabWindow 回退也失败: %s", exc)
            return None

    def valid_regions_for_paint(self):
        regions = list(self.selections)
        if self.selection_rect.isValid():
            regions.append(self.selection_rect)
        return regions

    def physical_rect(self, rect):
        if self._physical_origin is not None:
            # 物理像素模式：本地坐标与物理像素 1:1，仅需平移虚拟桌面原点。
            ox, oy = self._physical_origin
            physical = (rect.x() + ox, rect.y() + oy, rect.width(), rect.height())
            return physical_to_input_rect(physical) or physical
        # System-DPI fallback: with Qt high-DPI geometry disabled, the widget's
        # global coordinates are already the same virtualized input coordinates
        # consumed by SetCursorPos. Do not convert them to physical pixels here.
        origin = self.screen_mapper.virtual_rect.topLeft()
        return (
            rect.x() + origin.x(),
            rect.y() + origin.y(),
            rect.width(),
            rect.height(),
        )

    def instruction_title(self):
        return self.PURPOSE_COPY[self.purpose][0]

    def instruction_lines(self):
        primary = self.PURPOSE_COPY[self.purpose][1]
        if self.multi:
            return (
                "左键拖动可连续添加多个区域；区域相近时建议合并为一个大区域",
                "右键或 Esc 完成选择",
            )
        return primary, "右键或 Esc 取消"

    def instruction_panel_rect(self):
        margin = max(18, min(32, self.width() // 50))
        width = min(680, max(430, int(self.width() * 0.38)))
        width = min(width, max(260, self.width() - margin * 2))
        text_width = max(120, width - 50)
        title_font = QFont(self.font())
        title_font.setPointSize(max(1, int(round(20 * _ui_scale()))))
        title_font.setWeight(QFont.DemiBold)
        body_font = QFont(self.font())
        body_font.setPointSize(max(1, int(round(11 * _ui_scale()))))
        first_line, second_line = self.instruction_lines()
        title_height = QFontMetrics(title_font).height()
        first_height = QFontMetrics(body_font).boundingRect(
            QRect(0, 0, text_width, 1000),
            Qt.AlignLeft | Qt.TextWordWrap,
            first_line,
        ).height()
        second_height = QFontMetrics(body_font).boundingRect(
            QRect(0, 0, text_width, 1000),
            Qt.AlignLeft | Qt.TextWordWrap,
            second_line,
        ).height()
        height = 16 + title_height + 12 + first_height + 10 + second_height + 16
        return QRect(margin, max(margin, self.height() - height - margin), width, height)

    def instruction_panel_visible(self):
        panel = self.instruction_panel_rect()
        return not panel.adjusted(-52, -52, 52, 52).contains(self.current_pos)

    @staticmethod
    def _set_font(painter, source_font, point_size, weight=QFont.Normal):
        font = QFont(source_font)
        font.setPointSize(max(1, int(round(point_size * _ui_scale()))))
        font.setWeight(weight)
        painter.setFont(font)

    def _draw_instruction_panel(self, painter):
        if not self.instruction_panel_visible():
            return
        panel = self.instruction_panel_rect()
        shadow = panel.translated(0, 5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*INPUT_PANEL_SHADOW))
        painter.drawRoundedRect(shadow, 7, 7)
        painter.setBrush(QColor(*INPUT_PANEL_BG))
        painter.setPen(QPen(QColor(*INPUT_PANEL_BORDER), 2))
        painter.drawRoundedRect(panel, 7, 7)

        accent = QRect(panel.left(), panel.top(), 8, panel.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*INPUT_PANEL_ACCENT))
        painter.drawRoundedRect(accent, 4, 4)

        content = panel.adjusted(28, 16, -22, -14)
        self._set_font(painter, self.font(), 20, QFont.DemiBold)
        painter.setPen(QColor(*INPUT_PANEL_TITLE))
        title_height = painter.fontMetrics().height()
        painter.drawText(
            QRect(content.left(), content.top(), content.width(), title_height),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.instruction_title(),
        )
        first_line, second_line = self.instruction_lines()
        self._set_font(painter, self.font(), 11, QFont.Medium)
        painter.setPen(QColor(*INPUT_PANEL_BODY))
        first_rect = painter.fontMetrics().boundingRect(
            QRect(
                content.left(),
                content.top() + title_height + 12,
                content.width(),
                1000,
            ),
            Qt.AlignLeft | Qt.TextWordWrap,
            first_line,
        )
        painter.drawText(
            first_rect,
            Qt.AlignLeft | Qt.TextWordWrap,
            first_line,
        )
        self._set_font(painter, self.font(), 11, QFont.Normal)
        painter.setPen(QColor(*INPUT_PANEL_MUTED))
        second_top = first_rect.bottom() + 10
        painter.drawText(
            QRect(
                content.left(),
                second_top,
                content.width(),
                max(1, content.bottom() - second_top + 1),
            ),
            Qt.AlignLeft | Qt.TextWordWrap,
            second_line,
        )

    def _draw_selection(self, painter, rect, index, active=False):
        if not rect.isValid():
            return
        border = QColor(37, 99, 235, 250)
        painter.setBrush(QColor(37, 99, 235, 24))
        painter.setPen(
            QPen(border, 3, Qt.DashLine if active else Qt.SolidLine)
        )
        painter.drawRect(rect)

        handle_size = 8
        painter.setBrush(QColor(255, 255, 255, 250))
        painter.setPen(QPen(border, 2))
        for point in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
            painter.drawRect(
                QRect(
                    point.x() - handle_size // 2,
                    point.y() - handle_size // 2,
                    handle_size,
                    handle_size,
                )
            )

        _real_x, _real_y, real_w, real_h = self.physical_rect(rect)
        prefix = f"区域 {index}" if self.multi else "选区"
        info_text = f"{prefix}   {real_w} × {real_h} px"
        self._set_font(painter, self.font(), 10, QFont.DemiBold)
        metrics = painter.fontMetrics()
        badge_width = metrics.horizontalAdvance(info_text) + 22
        badge_height = 28
        badge_x = max(8, min(rect.left(), self.width() - badge_width - 8))
        badge_y = rect.top() - badge_height - 8
        if badge_y < 8:
            badge_y = min(self.height() - badge_height - 8, rect.bottom() + 8)
        badge = QRect(badge_x, badge_y, badge_width, badge_height)
        painter.setBrush(QColor(250, 252, 255, 244))
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(badge, 5, 5)
        painter.setPen(QColor(30, 64, 175))
        painter.drawText(badge, Qt.AlignCenter, info_text)

    def _draw_cursor_position(self, painter):
        if self._physical_origin is not None:
            # 物理像素模式：本地坐标 + 虚拟桌面原点 = 物理坐标（1:1）。
            ox, oy = self._physical_origin
            physical_x = self.current_pos.x() + ox
            physical_y = self.current_pos.y() + oy
            display_x, display_y = physical_to_input_coordinates(
                physical_x, physical_y
            )
        else:
            origin = self.screen_mapper.virtual_rect.topLeft()
            display_x = self.current_pos.x() + origin.x()
            display_y = self.current_pos.y() + origin.y()
        painter.setPen(QPen(QColor(37, 99, 235, 210), 2))
        painter.drawLine(
            self.current_pos.x() - 11,
            self.current_pos.y(),
            self.current_pos.x() + 11,
            self.current_pos.y(),
        )
        painter.drawLine(
            self.current_pos.x(),
            self.current_pos.y() - 11,
            self.current_pos.x(),
            self.current_pos.y() + 11,
        )

        text = f"{display_x}, {display_y}"
        self._set_font(painter, self.font(), 10, QFont.DemiBold)
        width = painter.fontMetrics().horizontalAdvance(text) + 20
        label_x = self.current_pos.x() + 16
        if label_x + width > self.width() - 8:
            label_x = self.current_pos.x() - width - 16
        label_y = self.current_pos.y() + 16
        if label_y + 28 > self.height() - 8:
            label_y = self.current_pos.y() - 36
        label = QRect(max(8, label_x), max(8, label_y), width, 28)
        painter.setBrush(QColor(23, 32, 51, 225))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(label, 5, 5)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(label, Qt.AlignCenter, text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # DESIGN: 物理像素模式下窗口几何与 GDI 截图同为物理像素尺寸，
        # 直接 1:1 绘制（drawPixmap(0, 0, ...)）即可完整覆盖；回退到逻辑
        # 坐标路径时仍按 self.rect() 缩放绘制，保持旧行为不变。
        physical_mode = self._physical_origin is not None

        # Draw frozen desktop snapshot as background, then overlay.
        if self._background is not None and not self._background.isNull():
            if physical_mode:
                painter.drawPixmap(0, 0, self._background)
            else:
                painter.drawPixmap(self.rect(), self._background, self._background.rect())
        painter.fillRect(self.rect(), QColor(15, 23, 42, 112))

        regions = self.valid_regions_for_paint()
        for index, rect in enumerate(regions, 1):
            # Re-paint the frozen snapshot inside each selection so the live
            # desktop does not show through while picking. Design request:
            # nothing on screen should update until the selection ends.
            if self._background is not None and not self._background.isNull():
                painter.save()
                painter.setClipRect(rect)
                if physical_mode:
                    painter.drawPixmap(0, 0, self._background)
                else:
                    painter.drawPixmap(
                        self.rect(), self._background, self._background.rect()
                    )
                painter.restore()
            self._draw_selection(
                painter,
                rect,
                index,
                active=rect is self.selection_rect,
            )
        self._draw_instruction_panel(painter)
        self._draw_cursor_position(painter)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_point = self._clamped_to_restrict(event.pos())
            self.selection_rect = QRect()
            self.update()
        elif event.button() == Qt.RightButton:
            if self.multi and self.selections:
                self.regions_selected.emit([self.physical_rect(rect) for rect in self.selections])
            self.close()

    def mouseMoveEvent(self, event):
        self.current_pos = event.pos()
        if self.start_point:
            self.end_point = self._clamped_to_restrict(event.pos())
            self.selection_rect = QRect(self.start_point, self.end_point).normalized()
        # Qt coalesces multiple update() calls within the same event loop
        # iteration into a single repaint, so this does not cause per-pixel redraws.
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.start_point:
            rect = self.selection_rect
            if rect.width() > 10 and rect.height() > 10:
                if self.multi:
                    self.selections.append(QRect(rect))
                    self.selection_rect = QRect()
                    self.start_point = None
                    self.end_point = None
                    self.update()
                else:
                    self.region_selected.emit(self.physical_rect(rect))
                    self.close()
                    return
            self.selection_rect = QRect()
            self.start_point = None
            self.end_point = None
            self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.multi and self.selections:
                self.regions_selected.emit(
                    [self.physical_rect(rect) for rect in self.selections]
                )
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

class MultiPointPickerUI(QWidget):
    def __init__(self, point_callback, finish_callback=None, initial_points=None):
        super().__init__()
        self.point_callback = point_callback
        self.finish_callback = finish_callback
        self.points = [tuple(p) for p in (initial_points or [])]
        self._finish_emitted = False
        self.current_pos = QPoint(0, 0)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

        self.screen_mapper = ScreenCoordinateMapper()
        self.screen_mapper.apply_to(self)
        self.show()
        self.raise_()
        self.activateWindow()

    def logical_to_input(self, point):
        physical = self.screen_mapper.local_to_physical(point)
        return physical_to_input_coordinates(*physical)

    def input_to_logical(self, x, y):
        physical = input_to_physical_coordinates(x, y)
        return self.screen_mapper.physical_to_local(*physical)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 85))

        painter.setPen(QColor(255, 255, 255))
        painter.setFont(_scaled_arial(15, bold=True))
        hint = f"左键连续添加点位 | 右键/Esc完成 | 已添加 {len(self.points)} 个点"
        fm = painter.fontMetrics()
        painter.drawText((self.width() - fm.horizontalAdvance(hint)) // 2, 80, hint)

        painter.setFont(_scaled_arial(10, bold=True))
        for idx, (x, y) in enumerate(self.points, 1):
            p = self.input_to_logical(x, y)
            painter.setBrush(QColor(255, 193, 7, 235))
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawEllipse(p, 8, 8)
            label_rect = QRect(p.x() + 10, p.y() - 20, 44, 20)
            painter.fillRect(label_rect, QColor(0, 0, 0, 165))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(label_rect, Qt.AlignCenter, str(idx))

        x, y = self.logical_to_input(self.current_pos)
        painter.setPen(QColor(255, 255, 0))
        painter.setFont(_scaled_arial(10))
        painter.drawText(self.current_pos.x() + 18, self.current_pos.y() + 28, f"Pos: {x},{y}")

    def mouseMoveEvent(self, event):
        self.current_pos = event.position().toPoint()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            x, y = self.logical_to_input(event.position().toPoint())
            self.points.append((x, y))
            if self.point_callback:
                self.point_callback(f"{x},{y}")
            self.update()
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self.close()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self.finish_callback and not self._finish_emitted:
            self._finish_emitted = True
            self.finish_callback(self.points)
        super().closeEvent(event)

class HotkeyCaptureHookThread(QThread):
    """Capture one complete combination before Qt can consume Alt/Ctrl events."""

    captured = Signal(str)
    cancelled = Signal()
    progress = Signal(str)
    ready = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.thread_id = None
        self.keyboard_hook = None
        self.pressed_vks = set()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.install_error = ""
        self.completed = False

    def _announce_ready(self, ok, error=""):
        """Set ready_event and emit the ready signal exactly once."""
        if self.ready_event.is_set():
            return
        self.ready_event.set()
        self.ready.emit(ok, error)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        if self.stop_event.is_set():
            self._announce_ready(False, "")
            return
        try:
            self.kb_pointer = HOOKPROC(self.keyboard_handler)
            self.keyboard_hook = win32_hook_backend.install_keyboard(self.kb_pointer)
            if not self.keyboard_hook:
                self.install_error = "无法启用底层键盘录入，已切换到兼容模式。"
                return
            self._announce_ready(True, "")
            while (
                not self.stop_event.is_set()
                and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0
            ):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._announce_ready(False, self.install_error)
            if getattr(self, "keyboard_hook", None):
                win32_hook_backend.uninstall(self.keyboard_hook)
                self.keyboard_hook = None

    def stop(self):
        self.stop_event.set()
        if getattr(self, "thread_id", None):
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def keyboard_handler(self, nCode, wParam, lParam):
        if self.stop_event.is_set():
            return win32_hook_backend.call_next(None, nCode, wParam, lParam)
        if nCode < 0:
            return win32_hook_backend.call_next(None, nCode, wParam, lParam)

        event = int(wParam)
        struct = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = int(struct.vkCode)
        if event in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self.pressed_vks.add(vk)
            if not self.completed:
                self.progress.emit(pressed_hotkey_display_text(self.pressed_vks))
            if not self.completed and vk not in MODIFIER_VKS:
                hotkey_text = hotkey_text_from_pressed_vks(vk, self.pressed_vks)
                parsed = parse_hotkey_text(hotkey_text)
                if parsed:
                    self.completed = True
                    if parsed["bare"] and parsed["key"] == "esc":
                        self.cancelled.emit()
                    else:
                        self.captured.emit(parsed["text"])
            return 1
        if event in (WM_KEYUP, WM_SYSKEYUP):
            self.pressed_vks.discard(vk)
            if not self.completed:
                self.progress.emit(pressed_hotkey_display_text(self.pressed_vks))
            return 1
        return win32_hook_backend.call_next(None, nCode, wParam, lParam)


class KeyCaptureDialog(QDialog):
    def __init__(self, parent=None, title="录入按键"):
        super().__init__(parent)
        self.captured_text = ""
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(360, 150)
        layout = QVBoxLayout(self)
        info = QLabel(
            "请直接按下要录入的按键或组合键。\n"
            "例如：A、Enter、Alt+A、Ctrl+Shift+S、Ctrl+Alt+Shift+A。\n"
            "按 Esc 取消。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.preview_label = QLabel("正在准备底层按键录入...")
        self.preview_label.setAlignment(Qt.AlignCenter)
        preview_font = QFont(self.preview_label.font())
        preview_font.setPointSizeF(max(13.0, QApplication.font().pointSizeF() * 1.65))
        preview_font.setBold(True)
        self.preview_label.setFont(preview_font)
        self.preview_label.setStyleSheet(f"color: {COLOR_PRIMARY};")
        layout.addWidget(self.preview_label)
        btns = QDialogButtonBox(QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.capture_thread = HotkeyCaptureHookThread()
        self.capture_thread.captured.connect(self.on_native_captured)
        self.capture_thread.cancelled.connect(self.reject)
        self.capture_thread.progress.connect(self.on_native_progress)
        self.capture_thread.ready.connect(self._on_capture_ready)
        self.capture_thread.start()

    def _on_capture_ready(self, ok, error):
        if ok:
            self.preview_label.setText("等待按键...")
        else:
            self.preview_label.setText(
                error or "底层录入启动较慢，请直接按键"
            )

    def on_native_progress(self, text):
        if self.captured_text:
            return
        self.preview_label.setText(f"当前按下：{text}" if text else "等待按键...")

    def on_native_captured(self, text):
        parsed = parse_hotkey_text(text)
        if not parsed:
            self.preview_label.setText("请再按一个非修饰键")
            return
        self.captured_text = parsed["text"]
        self.preview_label.setText(f"已录入：{parsed['display']}")
        schedule_widget_callback(self, 220, lambda owner: owner.accept())

    def keyPressEvent(self, event):
        if getattr(self, "capture_thread", None) and self.capture_thread.keyboard_hook:
            event.accept()
            return
        text = key_event_to_hotkey_text(event)
        if not text:
            self.reject()
            return
        parsed = parse_hotkey_text(text)
        if not parsed:
            self.preview_label.setText("请再按一个非修饰键")
            return
        self.captured_text = parsed["text"]
        self.preview_label.setText(f"已录入：{parsed['display']}")
        schedule_widget_callback(self, 220, lambda owner: owner.accept())

    def done(self, result):
        thread = getattr(self, "capture_thread", None)
        if thread and thread.isRunning():
            thread.stop()
            if not thread.wait(1500):
                write_log("热键录入钩子仍在停止中。",
                             category=LOG_ACTION,
                         )
        super().done(result)

class KeyMappingHookThread(QThread):
    triggered = Signal(str)
    failed = Signal(str)

    def __init__(self, hotkey_texts=None):
        super().__init__()
        self.hotkey_texts = set(hotkey_texts or [])
        self.is_active = False
        self.thread_id = None
        self.keyboard_hook = None
        self.pressed_vks = set()
        self.triggered_vks = set()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.install_error = None

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        if self.stop_event.is_set():
            self.ready_event.set()
            return

        try:
            self.kb_pointer = HOOKPROC(self.keyboard_handler)
            self.keyboard_hook = win32_hook_backend.install_keyboard(self.kb_pointer)
            if not self.keyboard_hook:
                # SAFETY: 安装失败必须可见（约束 #90 不得静默丢弃失败项），
                # 否则用户绑定的裸键映射无响应且无从排查（INP-FR-001）。
                self.install_error = (
                    "低级键盘钩子安装失败（可能被安全软件拦截），按键映射模式暂不可用"
                )
                self.failed.emit(self.install_error)
                return
            self.is_active = True
            self.ready_event.set()
            while not self.stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.is_active = False
            self.ready_event.set()
            if getattr(self, "keyboard_hook", None):
                win32_hook_backend.uninstall(self.keyboard_hook)
                self.keyboard_hook = None

    def stop(self):
        self.stop_event.set()
        self.is_active = False
        if getattr(self, "thread_id", None):
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def keyboard_handler(self, nCode, wParam, lParam):
        if self.stop_event.is_set():
            return win32_hook_backend.call_next(None, nCode, wParam, lParam)
        if nCode >= 0:
            struct = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(struct.vkCode)
            event = int(wParam)
            if event in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self.pressed_vks.add(vk)
                hotkey_text = hotkey_text_from_pressed_vks(vk, self.pressed_vks)
                if hotkey_text in self.hotkey_texts:
                    if vk not in self.triggered_vks:
                        self.triggered_vks.add(vk)
                        self.triggered.emit(hotkey_text)
                    return 1
            elif event in (WM_KEYUP, WM_SYSKEYUP):
                self.pressed_vks.discard(vk)
                if vk in self.triggered_vks:
                    self.triggered_vks.discard(vk)
                    return 1
        return win32_hook_backend.call_next(None, nCode, wParam, lParam)

class HookThread(QThread):
    finished_signal = Signal(list)
    ready_signal = Signal(bool, str)
    control_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()
        self.recording_event = threading.Event()
        self.ready_event = threading.Event()
        self.events_lock = threading.Lock()
        self.thread_id = None
        self.is_active = False
        self.mouse_hook = None
        self.keyboard_hook = None
        self.install_error = ""
        self.emit_result = False
        self.events = []
        # 录制长按语义：按键事件按下→松开的时间差必须可恢复。按下时记录事件引用，
        # 抬起时回填 `ended`（元组不可变，故事件用 list）。
        self._recording_key_events = {}
        # 批次 2：与语义事件并存的原始事件流，用于还原真实按下/释放顺序、
        # 自动重复与多键重叠。语义事件列表保持不变，旧转换路径不受影响。
        self.raw_input_events = []
        self.raw_events_truncated = False
        self._raw_key_down = set()
        # 原始流中仍按住的鼠标按钮：结束录制/截断时必须同样补 up，
        # 否则拖拽缺少 up 事件，转换端无法识别该拖拽、采样点会被丢弃。
        self._raw_mouse_down = set()
        # 拖拽轨迹采样：仅在按钮按住时记录，按「时间 + 距离」双阈值节流，
        # 避免鼠标移动把事件流灌满（普通移动不记录）。
        self._last_move_sample = None
        self.l_down_pos = None
        self.r_down_pos = None
        self.l_down_time = 0.0
        self.r_down_time = 0.0
        self.pressed_vks = set()
        self.control_pressed_vks = set()

    @staticmethod
    def _install_failure(label):
        try:
            error_code = int(kernel32.GetLastError())
        except (AttributeError, TypeError, ValueError):
            error_code = 0
        suffix = f"（Windows 错误码 {error_code}）" if error_code else ""
        return f"{label}{suffix}"

    def _announce_ready(self, success, message=""):
        if self.ready_event.is_set():
            return
        self.ready_event.set()
        self.ready_signal.emit(bool(success), str(message or ""))

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        if self.stop_event.is_set():
            self._announce_ready(False, "录制已取消。")
            return
        try:
            self.mouse_pointer = HOOKPROC(self.mouse_handler)
            self.mouse_hook = win32_hook_backend.install_mouse(self.mouse_pointer)
            if not self.mouse_hook:
                self.install_error = self._install_failure("无法启用鼠标录制钩子")
                return
            self.kb_pointer = HOOKPROC(self.keyboard_handler)
            self.keyboard_hook = win32_hook_backend.install_keyboard(self.kb_pointer)
            if not self.keyboard_hook:
                self.install_error = self._install_failure("无法启用键盘录制钩子")
                return
            self.is_active = True
            self._announce_ready(True)
            while (
                not self.stop_event.is_set()
                and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0
            ):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.is_active = False
            self.recording_event.clear()
            if not self.ready_event.is_set():
                self._announce_ready(
                    False,
                    self.install_error or "操作录制钩子未能启动。",
                )
            if getattr(self, "mouse_hook", None):
                win32_hook_backend.uninstall(self.mouse_hook)
                self.mouse_hook = None
            if getattr(self, "keyboard_hook", None):
                win32_hook_backend.uninstall(self.keyboard_hook)
                self.keyboard_hook = None
            if self.emit_result:
                self.truncate_pending_key_events()
                with self.events_lock:
                    events = list(self.events)
                self.finished_signal.emit(events)

    def _append_raw_event(self, event):
        """Append one raw hook event, enforcing the shared event ceiling.

        Caller must hold ``self.events_lock`` (the hook callbacks and the
        recording-end truncation both do).

        The semantic event list is capped in ``recording_model``; the raw
        stream needs its own cap because auto-repeat can append thousands of
        entries. Overflow stops appending and sets a flag so the truncation
        is observable instead of silent.
        """
        if self.raw_events_truncated:
            return
        if len(self.raw_input_events) >= MAX_RECORDED_EVENTS:
            self.raw_events_truncated = True
            _logger.warning(
                "录制原始事件达到上限 %d，后续事件不再记录（时间线将被截断）",
                MAX_RECORDED_EVENTS,
            )
            # DESIGN: 截断后仍有按住的键或鼠标按钮时，必须在截断点补 up。
            # 否则时间线以「未释放输入」结尾：校验会判为损坏并要求重新录制，
            # 而录制端明明已经接受了这次录制；拖拽缺 up 还会让采样点被丢弃。
            # 补 up 只写记录，不发 keyup / mouseup。
            self._close_open_raw_inputs(event[0])
            return
        self.raw_input_events.append(event)

    def _close_open_raw_inputs(self, moment):
        """Append release records for inputs still held at *moment*.

        Caller must hold ``self.events_lock``. Only records are written —
        the user may still be physically holding the key or button, and this
        program has no right to release it. The records exist so the raw
        stream stays self-consistent: a drag without an ``up`` event would be
        unreadable and its captured samples would be silently dropped.

        Args:
            moment: Timestamp used for every synthesized release record.
        """
        for vk in sorted(self._raw_key_down):
            name = VK_MAP.get(vk)
            if name:
                self.raw_input_events.append([moment, "key", name, "up"])
        self._raw_key_down.clear()
        for button in sorted(self._raw_mouse_down):
            point = self._raw_mouse_position(button)
            self.raw_input_events.append(
                [moment, "mouse", button, "up", point[0], point[1]]
            )
        self._raw_mouse_down.clear()

    def _raw_mouse_position(self, button):
        """Return the last known position for a held button.

        Prefers the latest sampled move (the drag's current point); falls back
        to the press position so the synthesized ``up`` never invents a point
        the cursor never visited.
        """
        sample = self._last_move_sample
        if sample is not None:
            return sample[1], sample[2]
        press = self.l_down_pos if button == "left" else self.r_down_pos
        if press:
            return press[0], press[1]
        return 0, 0

    def truncate_pending_key_events(self, now=None):
        """结束录制：把仍未抬起的按键事件在结束点截断并补齐释放记录。

        P5（录制长按语义）：只补「本程序记录到的」事件的 `ended`，使时间线自洽；
        **绝不**释放用户仍物理按住的键——本程序无权替用户松键。
        回放结束/异常/急停释放的是本程序注入的输入，与此处互不相干。
        """
        with self.events_lock:
            moment = time.monotonic() if now is None else float(now)
            for pending in self._recording_key_events.values():
                pending[3] = moment
            self._recording_key_events.clear()
            # 原始流同样在结束点截断：仍未抬起的键/鼠标按钮补 up 记录，
            # 只补记录，绝不向系统发送 keyup / mouseup（用户可能仍物理按住）。
            # 直接追加（绕过上限）：结束点补 up 是收尾记录，必须写入，
            # 否则时间线会以未释放输入结尾而被校验拒绝，拖拽采样点也会丢失。
            self._close_open_raw_inputs(moment)

    def begin_recording(self):
        if not self.is_active or self.stop_event.is_set():
            return False
        with self.events_lock:
            self.events.clear()
            self._recording_key_events.clear()
            self.raw_input_events.clear()
            self.raw_events_truncated = False
            self._raw_key_down.clear()
            self._raw_mouse_down.clear()
            self._last_move_sample = None
            self.l_down_pos = None
            self.r_down_pos = None
            self.l_down_time = 0.0
            self.r_down_time = 0.0
        self.recording_event.set()
        return True

    def finish_recording(self):
        if self.stop_event.is_set():
            return False
        self.recording_event.clear()
        self.emit_result = True
        self.stop()
        return True

    def stop(self):
        self.stop_event.set()
        self.recording_event.clear()
        self.is_active = False
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def mouse_handler(self, nCode, wParam, lParam):
        if self.stop_event.is_set() or not self.recording_event.is_set():
            return win32_hook_backend.call_next(None, nCode, wParam, lParam)
        if nCode >= 0:
            struct = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            raw_x, raw_y = int(struct.pt.x), int(struct.pt.y)
            if wParam in (0x0201, 0x0202, 0x0204, 0x0205):
                x, y = physical_to_input_coordinates(raw_x, raw_y)
            else:
                x, y = raw_x, raw_y

            if wParam == 0x0201: # WM_LBUTTONDOWN
                self.l_down_pos = (x, y)
                self.l_down_time = time.monotonic()
                with self.events_lock:
                    self._append_raw_event(
                        [self.l_down_time, "mouse", "left", "down", x, y]
                    )
                    self._raw_mouse_down.add("left")
            elif wParam == 0x0202: # WM_LBUTTONUP
                if getattr(self, 'l_down_pos', None):
                    dx = abs(x - self.l_down_pos[0])
                    dy = abs(y - self.l_down_pos[1])
                    if dx > _DRAG_THRESHOLD_PX or dy > _DRAG_THRESHOLD_PX:
                        event = (self.l_down_time, 'left_drag', (*self.l_down_pos, x, y), time.monotonic())
                    else:
                        event = (self.l_down_time, 'left', (x, y), time.monotonic())
                    with self.events_lock:
                        self.events.append(event)
                        self._append_raw_event(
                            [event[3], "mouse", "left", "up", x, y]
                        )
                        self._raw_mouse_down.discard("left")
                    self.l_down_pos = None
                    self._last_move_sample = None
            elif wParam == 0x0204: # WM_RBUTTONDOWN
                self.r_down_pos = (x, y)
                self.r_down_time = time.monotonic()
                with self.events_lock:
                    self._append_raw_event(
                        [self.r_down_time, "mouse", "right", "down", x, y]
                    )
                    self._raw_mouse_down.add("right")
            elif wParam == 0x0205: # WM_RBUTTONUP
                if getattr(self, 'r_down_pos', None):
                    dx = abs(x - self.r_down_pos[0])
                    dy = abs(y - self.r_down_pos[1])
                    if dx > _DRAG_THRESHOLD_PX or dy > _DRAG_THRESHOLD_PX:
                        event = (self.r_down_time, 'right_drag', (*self.r_down_pos, x, y), time.monotonic())
                    else:
                        event = (self.r_down_time, 'right', (x, y), time.monotonic())
                    with self.events_lock:
                        self.events.append(event)
                        self._append_raw_event(
                            [event[3], "mouse", "right", "up", x, y]
                        )
                        self._raw_mouse_down.discard("right")
                    self.r_down_pos = None
                    self._last_move_sample = None
            elif wParam == 0x0200: # WM_MOUSEMOVE
                # 只在按住鼠标键时记录（拖拽轨迹）；普通移动不入时间线。
                if self.l_down_pos or self.r_down_pos:
                    mx, my = physical_to_input_coordinates(raw_x, raw_y)
                    now = time.monotonic()
                    previous = self._last_move_sample
                    if (
                        previous is None
                        or now - previous[0] >= 0.02
                        or abs(mx - previous[1]) >= 3
                        or abs(my - previous[2]) >= 3
                    ):
                        self._last_move_sample = (now, mx, my)
                        with self.events_lock:
                            self._append_raw_event([now, "move", mx, my])
            elif wParam == 0x020A: # WM_MOUSEWHEEL
                delta = ctypes.c_short(struct.mouseData >> 16).value
                now = time.monotonic()
                with self.events_lock:
                    self.events.append((now, 'scroll', delta, now))
                    self._append_raw_event([now, "wheel", delta])
        return win32_hook_backend.call_next(None, nCode, wParam, lParam)

    def keyboard_handler(self, nCode, wParam, lParam):
        if nCode >= 0:
            struct = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(struct.vkCode)
            event_type = int(wParam)
            if vk in (VK_F8, VK_ESCAPE):
                if event_type in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self.control_pressed_vks.add(vk)
                elif event_type in (WM_KEYUP, WM_SYSKEYUP):
                    was_pressed = vk in self.control_pressed_vks
                    self.control_pressed_vks.discard(vk)
                    if was_pressed and not self.stop_event.is_set():
                        self.control_signal.emit("toggle" if vk == VK_F8 else "cancel")
                # DESIGN: stop_event 置位到钩子实际卸载之间存在 <100ms 窗口，
                # 此期间继续吞噬 F8/Escape 是有意为之，防止 stop 后立即
                # 触发下一次录制启动。窗口极短，对用户无感知影响。
                return 1
            if self.stop_event.is_set():
                return win32_hook_backend.call_next(None, nCode, wParam, lParam)
            if event_type in (WM_KEYDOWN, WM_SYSKEYDOWN):
                repeated = vk in self.pressed_vks
                self.pressed_vks.add(vk)
                if self.recording_event.is_set():
                    # 一次取时刻，供原始流与语义事件共用：两者描述同一按键动作，
                    # 分别取时会让「按下时刻」出现微小漂移。
                    now = time.monotonic()
                    # 时间线采集：修饰键也按单键记录（VK_MAP 含 shift/ctrl/alt），
                    # 不折叠成组合名，否则组合内各键的真实顺序会丢失。
                    raw_name = VK_MAP.get(vk)
                    if raw_name:
                        with self.events_lock:
                            self._append_raw_event(
                                [now, "key", raw_name,
                                 "repeat" if repeated else "down"]
                            )
                            if not repeated:
                                self._raw_key_down.add(vk)
                    if not repeated and vk not in MODIFIER_VKS:
                        hotkey = hotkey_text_from_pressed_vks(vk, self.pressed_vks)
                        if hotkey:
                            with self.events_lock:
                                event = [now, 'hotkey', hotkey, now]
                                self.events.append(event)
                                self._recording_key_events[vk] = event
            elif event_type in (WM_KEYUP, WM_SYSKEYUP):
                self.pressed_vks.discard(vk)
                if self.recording_event.is_set():
                    now = time.monotonic()
                    raw_name = VK_MAP.get(vk)
                    if raw_name:
                        with self.events_lock:
                            self._append_raw_event([now, "key", raw_name, "up"])
                            self._raw_key_down.discard(vk)
                    # DESIGN: 录制长按语义——抬起时把该键事件的 `ended` 回填为当前时刻，
                    # 使「按住多久」可被保留；缺这一步会把长按压成瞬时事件，
                    # 回放只能靠松键后的等待近似，时序与原操作不等价。
                    with self.events_lock:
                        pending = self._recording_key_events.pop(vk, None)
                    if pending is not None:
                        pending[3] = now
        return win32_hook_backend.call_next(None, nCode, wParam, lParam)

class RecorderUI(QWidget):
    def __init__(
        self,
        main_window=None,
        *,
        tasks_callback=None,
        log_callback=None,
        restore_callback=None,
        hook_factory=HookThread,
    ):
        super().__init__()
        self.main_window = main_window
        self.tasks_callback = tasks_callback
        self.log_callback = log_callback
        self.restore_callback = restore_callback
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(320, 80)

        screen = QApplication.primaryScreen()
        if screen is not None:
            rect = screen.geometry()
            self.move((rect.width() - 320) // 2, 40)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.bg = QFrame()
        self.bg.setStyleSheet(
            f"background-color: {OVERLAY_BG}; border-radius: 8px; border: 2px solid {COLOR_FOCUS};"
        )
        self.layout.addWidget(self.bg)

        inner = QVBoxLayout(self.bg)
        self.label = QLabel("正在准备录制...\nF8 开始 | Esc 取消")
        self.label.setStyleSheet(
            f"color: white; font-size: {int(14 * _ui_scale())}px; font-weight: bold;"
        )
        self.label.setAlignment(Qt.AlignCenter)
        inner.addWidget(self.label)

        self.state = 0
        self.cancelled = False
        self.result_handled = False
        self.hook_thread = hook_factory()
        self.hook_thread.finished_signal.connect(self.on_recorded)
        self.hook_thread.ready_signal.connect(self.on_hook_ready)
        self.hook_thread.control_signal.connect(self.on_control_key)
        self.show()
        self.hook_thread.start()

    def on_hook_ready(self, success, message):
        if self.cancelled or self.result_handled:
            return
        if success:
            self.label.setText("准备录制\nF8 开始 | Esc 取消")
            return
        self.state = 3
        self.result_handled = True
        detail = str(message or "操作录制钩子未能启动。")
        self.label.setText(f"录制功能启动失败\n{detail}")
        self.bg.setStyleSheet(
            f"background-color: {OVERLAY_BG_DANGER}; border-radius: 8px; "
            f"border: 2px solid {COLOR_DANGER};"
        )
        self.append_recording_log(f"操作录制不可用：{detail}")
        self.restore_main_window()
        schedule_close(self, 2200)

    def on_control_key(self, action):
        if action == "cancel":
            self.abort()
        elif action == "toggle":
            if self.state == 0:
                self.start_recording()
            elif self.state == 1:
                self.stop_recording()

    def start_recording(self):
        if not self.hook_thread or not self.hook_thread.begin_recording():
            self.label.setText("录制器尚未准备好，请稍候再按 F8")
            return False
        self.cancelled = False
        self.result_handled = False
        self.state = 1
        self.label.setText("正在录制键鼠操作\nF8 停止并生成 | Esc 放弃")
        self.bg.setStyleSheet(
            f"background-color: {OVERLAY_BG_DANGER}; border-radius: 8px; border: 2px solid {COLOR_DANGER};"
        )
        return True

    def stop_recording(self):
        if self.state != 1:
            return False
        self.state = 2
        self.label.setText("正在生成指令...")
        return bool(
            self.hook_thread and self.hook_thread.finish_recording()
        )

    def abort(self):
        self.cancelled = True
        self.result_handled = True
        if self.hook_thread:
            self.hook_thread.stop()
            self.hook_thread.wait(1500)
        self.restore_main_window()
        self.close()

    def on_recorded(self, events):
        if self.cancelled or self.result_handled:
            return
        self.result_handled = True
        double_click_seconds = max(0.1, user32.GetDoubleClickTime() / 1000.0)
        # 批次 2：原始事件流存在时优先判定是否需要用时间线表达时序
        # （自动重复 / 多键重叠）；否则沿用逐动作步骤，保持简单录制可读。
        raw_events = list(getattr(self.hook_thread, "raw_input_events", ()) or ())
        if getattr(self.hook_thread, "raw_events_truncated", False):
            self.append_recording_log(
                "操作录制：事件数达到上限，时间线已截断（末尾部分未记录）。"
            )
        tasks = build_recording_tasks(
            events,
            raw_events,
            double_click_seconds=double_click_seconds,
            # 批次 3：鼠标与键鼠重叠也参与时间线判定（C3/C5）。
            include_mouse=True,
        )

        added_count = None
        if callable(self.tasks_callback):
            added_count = self.tasks_callback(tasks)
        elif self.main_window is not None:
            add_rows = getattr(self.main_window, "add_rows", None)
            if callable(add_rows):
                added_count = add_rows(tasks)
            else:
                for task in tasks:
                    self.main_window.add_row(task)

        if tasks:
            if added_count == 0:
                # FR-901-002: 运行期编辑锁会把录制结果静默丢弃（add_rows 返回 0）。
                # 必须明确告知用户，不能继续显示"已生成 N 步指令"造成误导。
                self.append_recording_log(
                    "操作录制完成：正在运行脚本，本次录制结果未加入步骤列表，"
                    "请停止运行后重新录制。"
                )
            else:
                self.append_recording_log(f"操作录制完成：已生成 {len(tasks)} 步指令。")
        else:
            self.append_recording_log("操作录制完成：未录制到任何动作。")

        self.restore_main_window()
        self.close()

    def append_recording_log(self, message):
        if callable(self.log_callback):
            self.log_callback(message)
        elif self.main_window is not None:
            self.main_window.append_log(message)

    def restore_main_window(self):
        if callable(self.restore_callback):
            self.restore_callback()
        elif self.main_window is not None:
            self.main_window.showNormal()

    def closeEvent(self, event):
        if self.state != 2 and not self.result_handled:
            self.cancelled = True
            self.result_handled = True
        if self.hook_thread and self.hook_thread.isRunning():
            self.hook_thread.stop()
            if not self.hook_thread.wait(1500):
                self._close_retry_count = getattr(self, '_close_retry_count', 0) + 1
                if self._close_retry_count > 30:  # 30 * 100ms = 3s max
                    event.accept()
                    return
                write_log("录制钩子仍在停止中，暂缓关闭录制窗口。",
                             category=LOG_ACTION,
                         )
                event.ignore()
                schedule_close(self, 100)
                return
        event.accept()

class CoordinatePickThread(QThread):
    pos_signal = Signal(int, int)
    done_signal = Signal(str)
    cancelled_signal = Signal()
    failed = Signal(str)

    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.is_active = False
        self.down_pos = None
        # FR-013: 右键取消会置位 stop_event；WM_RBUTTONUP 必须由本标志单独
        # 吞掉，否则会落到 stop_event 分支被透传给目标窗口。但**未配对**的
        # 抬起（此前没有按下）仍须透传——吞掉它会让目标窗口永远等不到一次
        # 完整的右键。
        self._rbutton_swallow_up = False
        self.thread_id = None
        self.mouse_hook = None
        self.stop_event = threading.Event()
        self._last_emit_time = 0.0
        self.install_error = None

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        if self.stop_event.is_set():
            return
        try:
            self.mouse_pointer = HOOKPROC(self.mouse_handler)
            self.mouse_hook = win32_hook_backend.install_mouse(self.mouse_pointer)
            if not self.mouse_hook:
                # SAFETY: 安装失败必须可见；否则取点覆盖层滞留且“右键取消”
                # 提示依赖已死钩子（INP-FR-001）。
                self.install_error = (
                    "低级鼠标钩子安装失败（可能被安全软件拦截），取点不可用"
                )
                self.failed.emit(self.install_error)
                return
            self.is_active = True
            while not self.stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.is_active = False
            if getattr(self, 'mouse_hook', None):
                win32_hook_backend.uninstall(self.mouse_hook)
                self.mouse_hook = None

    def stop(self):
        self.stop_event.set()
        self.is_active = False
        if getattr(self, 'thread_id', None):
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def finish_with_value(self, value):
        self.done_signal.emit(value)
        self.stop()

    def cancel(self):
        self.cancelled_signal.emit()
        self.stop()

    def mouse_handler(self, nCode, wParam, lParam):
        if self.stop_event.is_set():
            return win32_hook_backend.call_next(None, nCode, wParam, lParam)
        if nCode >= 0:
            struct = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            now = time.monotonic()
            emit_position = now - self._last_emit_time >= 0.016
            needs_position = emit_position or wParam in (
                0x0201,
                0x0202,
                0x0204,
            )
            if needs_position:
                x, y = physical_to_input_coordinates(struct.pt.x, struct.pt.y)
            else:
                x, y = int(struct.pt.x), int(struct.pt.y)
            if emit_position:  # ~60fps throttle
                self._last_emit_time = now
                self.pos_signal.emit(x, y)

            if nCode >= 0 and wParam == 0x0205 and self._rbutton_swallow_up:
                # SAFETY/FR-013: 右键取消必须成对吞掉按下与抬起。cancel() 会置位
                # stop_event，因此抬起若留给下方 "stop_event → call_next" 分支，目标
                # 窗口会收到一个没有配对按下的 WM_RBUTTONUP；Windows 的 DefWindowProc
                # 会据此合成 WM_CONTEXTMENU，表现为"右键取消取点器时目标程序弹出右键
                # 菜单"。故抬起判定必须放在 stop_event 早退之前。
                self._rbutton_swallow_up = False
                return 1

            if wParam == 0x0204:  # WM_RBUTTONDOWN
                # FR-013: 抬起必须由本标志单独吞掉，否则会落到 stop_event 分支
                # 被透传给目标窗口。
                self._rbutton_swallow_up = True
                self.cancel()
                return 1

            if self.mode in ("point", "window"):
                if wParam == 0x0201:  # WM_LBUTTONDOWN
                    self.down_pos = (x, y)
                    return 1
                if wParam == 0x0202 and self.down_pos:
                    px, py = self.down_pos
                    self.down_pos = None
                    self.finish_with_value(f"{px},{py}")
                    return 1
            else:
                if wParam == 0x0201:
                    self.down_pos = (x, y)
                    return 1
                if wParam == 0x0202 and self.down_pos:
                    x1, y1 = self.down_pos
                    self.down_pos = None
                    self.finish_with_value(f"{x1},{y1} -> {x},{y}")
                    return 1

        return win32_hook_backend.call_next(None, nCode, wParam, lParam)

class CoordinatePickerUI(QWidget):
    def __init__(self, mode, callback, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.callback = callback
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.bg = QFrame()
        self.bg.setStyleSheet(
            f"background-color: {OVERLAY_BG}; border-radius: 8px; border: 2px solid {COLOR_FOCUS};"
        )
        layout.addWidget(self.bg)

        inner = QVBoxLayout(self.bg)
        inner.setContentsMargins(12, 10, 12, 10)
        hint = self.hint_text()
        self.label = QLabel(f"{hint}\n当前鼠标位置: --,--\n右键取消")
        self.label.setStyleSheet(
            f"color: white; font-size: {int(13 * _ui_scale())}px; font-weight: bold;"
        )
        self.label.setAlignment(Qt.AlignCenter)
        inner.addWidget(self.label)
        try:
            px, py = pyautogui.position()
            self.update_pos(px, py)
        except Exception as exc:
            _logger.debug("更新坐标失败: %s", exc, exc_info=True)
            pass

        self.pick_thread = CoordinatePickThread(mode)
        self.pick_thread.pos_signal.connect(self.update_pos)
        self.pick_thread.done_signal.connect(self.finish_pick)
        self.pick_thread.cancelled_signal.connect(self.cancel_pick)
        self.pick_thread.failed.connect(self._on_hook_failed)
        self.pick_thread.start()

        self.resize(300, 92)
        self.move_to_top()
        self.show()

    def move_to_top(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        self.move(rect.x() + (rect.width() - self.width()) // 2, rect.y() + 24)

    def update_pos(self, x, y):
        hint = self.hint_text()
        self.label.setText(f"{hint}\n当前鼠标位置: {x},{y}\n右键取消")

    def hint_text(self):
        if self.mode == "window":
            return "请左键单击目标程序窗口中的任意位置"
        if self.mode == "point":
            return "左键单击选取坐标"
        return "按住左键拖动，松开后选取轨迹"

    def finish_pick(self, value):
        with contextlib.suppress(RuntimeError):  # 控件已被虚拟化回收，安全忽略
            self.callback(value)
        self.close()

    def _on_hook_failed(self, reason):
        # SAFETY/INP-FR-001: 钩子安装失败时取点不可用：关闭覆盖层并留下可见
        # 原因，不能保留依赖已死钩子的“右键取消”提示。
        write_log(f"取点器启动失败：{reason}",
                     category=LOG_ACTION,
                     critical=True,
                 )
        self.cancel_pick()

    def cancel_pick(self):
        self.close()

    def closeEvent(self, event):
        if getattr(self, 'pick_thread', None) and self.pick_thread.isRunning():
            self.pick_thread.stop()
            if not self.pick_thread.wait(1500):
                self._close_retry_count = getattr(self, '_close_retry_count', 0) + 1
                if self._close_retry_count > 30:  # 30 * 100ms = 3s max
                    event.accept()
                    return
                write_log("坐标选取钩子仍在停止中，暂缓关闭选取窗口。",
                             category=LOG_ACTION,
                         )
                event.ignore()
                schedule_close(self, 100)
                return
        event.accept()
