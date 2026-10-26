# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false
"""Preview, running-status, and click-indicator overlays."""

import contextlib
import ctypes
import logging
import math
import time
import weakref
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

from ..coordinates import (
    ScreenCoordinateMapper,
    input_to_physical_coordinates,
    physical_to_input_coordinates,
)
from ..params import OverlayOptions
from ..task_model import parse_float_text
from .lifecycle import schedule_close, schedule_widget_callback
from .theme import (
    COLOR_FOCUS,
    INPUT_PANEL_BORDER,
    OVERLAY_BG,
    PREVIEW_LINE_ACTIVE,
    PREVIEW_LINE_BRANCH,
    PREVIEW_LINE_CROSS_GROUP,
    PREVIEW_LINE_CROSS_GROUP_HOVER,
    PREVIEW_LINE_SOLID,
)

logger = logging.getLogger(__name__)

WDA_EXCLUDEFROMCAPTURE = 0x00000011
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1

# Font family used across all overlay painters.
_OVERLAY_FONT_FAMILY = "Arial"

# Throttle the expensive point-moved callback while dragging a preview point
# so a single drag does not re-run it on every pixel (was ~170% CPU).
DRAG_CALLBACK_THROTTLE_NS = 80_000_000
HOVER_HIT_TEST_THROTTLE_NS = 16_000_000
_SCREEN_COORDINATE_LIMIT = 2_147_483_647
_INVALID_LOCAL_POINT = (-1_000_000, -1_000_000)


def _safe_screen_int(value):
    """Convert a screen coordinate without allowing Qt/Win32 overflow."""

    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or abs(numeric) > _SCREEN_COORDINATE_LIMIT:
        return None
    try:
        return int(round(numeric))
    except (OverflowError, ValueError):
        return None


def _input_to_local(mapper, x, y):
    """Map one engine/input coordinate to an overlay-local Qt point."""

    input_x = _safe_screen_int(x)
    input_y = _safe_screen_int(y)
    if input_x is None or input_y is None:
        return QPoint(*_INVALID_LOCAL_POINT)
    try:
        physical = input_to_physical_coordinates(input_x, input_y)
        physical_x = _safe_screen_int(physical[0])
        physical_y = _safe_screen_int(physical[1])
        if physical_x is None or physical_y is None:
            return QPoint(*_INVALID_LOCAL_POINT)
        return mapper.physical_to_local(physical_x, physical_y)
    except (AttributeError, OSError, OverflowError, RuntimeError, TypeError, ValueError):
        # SAFETY: malformed imported coordinates must never escape into Qt's
        # 32-bit QPoint conversion; the point is rendered outside the overlay.
        return QPoint(*_INVALID_LOCAL_POINT)


def _local_to_input(mapper, point):
    """Map one overlay-local Qt point back to the engine/input coordinate space."""

    physical = mapper.local_to_physical(point)
    return physical_to_input_coordinates(*physical)


def _ui_scale() -> float:
    """Current UI zoom factor (1.0 == 100%).

    Reads the app property set by ``theme.py`` (``_fukua_ui_scale``). Falls back
    to 1.0 when no ``QApplication`` is available (e.g. offscreen tests) so the
    helpers stay safe to call from any context (review item #10).
    """

    try:
        app = QApplication.instance()
        if app is not None:
            value = app.property("_fukua_ui_scale")
            if value:
                return max(0.5, min(3.0, float(value)))
    except Exception as exc:
        logger.debug("解析缩放值失败: %s", exc, exc_info=True)
        pass
    return 1.0


def _overlay_font(size, *, bold=False, weight=QFont.Normal):
    """Build an overlay font whose point size tracks the UI zoom factor."""

    return QFont(
        _OVERLAY_FONT_FAMILY,
        max(1, int(round(size * _ui_scale()))),
        QFont.Bold if bold else weight,
    )


def exclude_window_from_capture(widget):
    """Ask supported Windows builds to omit an owned overlay from captures."""

    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
        excluded = bool(
            user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        )
        widget.setProperty("captureExcluded", excluded)
        return excluded
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        widget.setProperty("captureExcluded", False)
        return False


class CoordinateStepPreviewOverlay(QWidget):
    _active_preview_ref = None

    def __init__(self, points, options=None, title=None, auto_close_ms=6000, draw_lines=True, editable_indices=None, point_moved_callback=None, detail_text=None, close_on_left_blank=True, marked_indices=None, point_labels=None, line_segments=None):
        super().__init__()
        previous = (
            self._active_preview_ref()
            if callable(self._active_preview_ref)
            else None
        )
        if previous is not None and previous is not self:
            with contextlib.suppress(RuntimeError):
                previous.close()
        type(self)._active_preview_ref = weakref.ref(self)
        self.points = [(float(x), float(y)) for x, y in points]
        self.options = options or {}
        self.custom_title = title
        self.detail_text = detail_text
        self.auto_close_ms = auto_close_ms
        self.draw_lines = draw_lines
        self.editable_indices = set(editable_indices or [])
        self.marked_indices = set(marked_indices or [])
        self.point_moved_callback = point_moved_callback
        self.point_released_callback = None
        self.line_released_callback = None
        self.close_on_left_blank = close_on_left_blank
        self.point_labels = list(point_labels or [])
        self.line_segments = list(line_segments or [])
        # DESIGN: 构造并保留 OverlayOptions 作为本次构造的聚合参数对象，便于
        # 后续（P5 MVC 拆分）在不改动其余展示逻辑的前提下接管参数来源；当前
        # 其余方法仍直接读取上面的 self.* 字段（深迁移留待 P5 与 overlay 拆分
        # 一并完成，避免本次无行为变更阶段引入大范围改漏风险）。
        self._overlay_options = OverlayOptions(
            points=points,
            options=options,
            title=title,
            auto_close_ms=auto_close_ms,
            draw_lines=draw_lines,
            editable_indices=editable_indices,
            point_moved_callback=point_moved_callback,
            detail_text=detail_text,
            close_on_left_blank=close_on_left_blank,
            marked_indices=marked_indices,
            point_labels=point_labels,
            line_segments=line_segments,
        )
        self.drag_index = None
        self.drag_segment_index = None
        self.hover_index = None
        self.hover_segment_index = None
        self._point_drag_original_points = None
        self._segment_drag_original_points = None
        self._segment_drag_start_input = None
        self._screen_points_cache = None
        self._last_drag_callback_ns = 0
        self._last_hover_hit_test_ns = 0
        # Coalesce repaints during a drag: a high-frequency mouse can fire far
        # more move events than the display refresh rate, and each update()
        # re-runs the full overlay paint. Throttle to ~one paint per frame so
        # the dragged marker still tracks the cursor but the CPU stays bounded.
        self._repaint_dirty = False
        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(16)
        self._repaint_timer.timeout.connect(self._flush_repaint)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

        self.screen_mapper = ScreenCoordinateMapper()
        self.screen_mapper.apply_to(self)

        if self.auto_close_ms > 0:
            schedule_close(self, self.auto_close_ms)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def to_screen_point(self, x, y):
        return _input_to_local(self.screen_mapper, x, y)

    def _schedule_update(self):
        """Schedule a coalesced repaint (at most one per timer interval)."""

        self._repaint_dirty = True
        if not self._repaint_timer.isActive():
            self._repaint_timer.start()

    def _flush_repaint(self):
        if self._repaint_dirty:
            self._repaint_dirty = False
            self.update()

    def set_points(self, points):
        self.points = [(float(x), float(y)) for x, y in points]
        self._screen_points_cache = None
        self.update()

    def set_marked_indices(self, marked_indices):
        self.marked_indices = set(marked_indices or [])
        self.update()

    def set_editable_indices(self, editable_indices):
        self.editable_indices = set(editable_indices or [])
        self.hover_index = None
        self.update()

    def set_point_labels(self, labels):
        self.point_labels = list(labels or [])
        self.update()

    def set_line_segments(self, segments):
        self.line_segments = list(segments or [])
        self.hover_segment_index = None
        self.update()

    def from_screen_point(self, point):
        return _local_to_input(self.screen_mapper, point)

    def nearest_editable_index(self, pos):
        if not self.editable_indices or not self.points:
            return None
        screen_points = self._screen_points()
        best_idx = None
        best_dist = 22 * 22
        for idx in self.editable_indices:
            if idx < 0:
                idx = len(screen_points) + idx
            if idx < 0 or idx >= len(screen_points):
                continue
            point = screen_points[idx]
            dist = (point.x() - pos.x()) ** 2 + (point.y() - pos.y()) ** 2
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _screen_points(self):
        if self._screen_points_cache is None:
            self._screen_points_cache = [
                self.to_screen_point(x, y) for x, y in self.points
            ]
        return self._screen_points_cache

    @staticmethod
    def _distance_to_segment_squared(pos, start, end):
        dx = float(end.x() - start.x())
        dy = float(end.y() - start.y())
        if not dx and not dy:
            return float(
                (pos.x() - start.x()) ** 2 + (pos.y() - start.y()) ** 2
            )
        ratio = (
            (pos.x() - start.x()) * dx + (pos.y() - start.y()) * dy
        ) / (dx * dx + dy * dy)
        ratio = max(0.0, min(1.0, ratio))
        nearest_x = start.x() + ratio * dx
        nearest_y = start.y() + ratio * dy
        return (pos.x() - nearest_x) ** 2 + (pos.y() - nearest_y) ** 2

    def nearest_segment_index(self, pos, *, include_blocked=False):
        if not self.line_segments or len(self.points) < 2:
            return None
        screen_points = self._screen_points()
        threshold = max(8.0, 10.0 * _ui_scale())
        best_index = None
        best_distance = threshold * threshold
        for index, segment in enumerate(self.line_segments):
            movable = bool(segment.get("move_indices"))
            blocked = bool(segment.get("blocked_reason"))
            if not movable and not (include_blocked and blocked):
                continue
            try:
                start_index = int(segment.get("from"))
                end_index = int(segment.get("to"))
            except (TypeError, ValueError):
                continue
            if not (
                0 <= start_index < len(screen_points)
                and 0 <= end_index < len(screen_points)
            ):
                continue
            start = screen_points[start_index]
            end = screen_points[end_index]
            if not (
                min(start.x(), end.x()) - threshold
                <= pos.x()
                <= max(start.x(), end.x()) + threshold
                and min(start.y(), end.y()) - threshold
                <= pos.y()
                <= max(start.y(), end.y()) + threshold
            ):
                continue
            distance = self._distance_to_segment_squared(pos, start, end)
            if distance <= best_distance:
                best_distance = distance
                best_index = index
        return best_index

    def _segment_move_indices(self, segment_index):
        if not 0 <= int(segment_index) < len(self.line_segments):
            return ()
        result = []
        for raw_index in self.line_segments[int(segment_index)].get(
            "move_indices", ()
        ):
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(self.points) and index not in result:
                result.append(index)
        return tuple(result)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if not self.points:
            return

        if self.custom_title:
            title = self.custom_title
        else:
            title = f"坐标步进预览：{len(self.points)} 个可轮到的点位"
            if self.auto_close_ms > 0:
                title += f"，{int(self.auto_close_ms / 1000)} 秒后自动关闭"
        if not self.custom_title and self.options.get("direction") == "移动到新点位":
            max_steps = max(0, int(parse_float_text(self.options.get("max_steps", 0), 0)))
            if max_steps >= 2:
                title += f"；路径点位 {max_steps} = 起点到目标点共 {max_steps} 个点"
            else:
                title += "；路径点位 0 = 只使用起点和目标点"
        s = _ui_scale()
        painter.setFont(_overlay_font(13, bold=True))
        fm = painter.fontMetrics()
        max_title_width = max(360, self.width() - 40)
        title = fm.elidedText(title, Qt.ElideRight, max_title_width - 24)
        detail = self.detail_text or ""
        min_title_width = 520 if detail else 0
        title_rect = QRect(
            20,
            24,
            min(max(fm.horizontalAdvance(title) + 24, min_title_width), max_title_width),
            int((58 if detail else 34) * s),
        )
        title_rect.moveLeft(max(20, (self.width() - title_rect.width()) // 2))
        painter.fillRect(title_rect, QColor(0, 0, 0, 190))
        painter.setPen(QColor(255, 255, 255))
        if detail:
            painter.drawText(QRect(title_rect.x() + 12, title_rect.y() + int(5 * s), title_rect.width() - 24, int(24 * s)), Qt.AlignCenter, title)
            painter.setFont(_overlay_font(9))
            dfm = painter.fontMetrics()
            draw_detail = dfm.elidedText(detail, Qt.ElideRight, title_rect.width() - 24)
            painter.drawText(QRect(title_rect.x() + 12, title_rect.y() + int(30 * s), title_rect.width() - 24, int(22 * s)), Qt.AlignCenter, draw_detail)
        else:
            painter.drawText(title_rect, Qt.AlignCenter, title)

        screen_points = self._screen_points()
        normalized_editable = {
            len(screen_points) + idx if idx < 0 else idx
            for idx in self.editable_indices
        }
        # Fully transparent pixels in a layered Windows window can be treated
        # as click-through. A nearly invisible disk gives every editable point
        # a reliable 22 px native hit target without enlarging its marker.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 2))
        for idx in normalized_editable:
            if 0 <= idx < len(screen_points):
                painter.drawEllipse(screen_points[idx], 22, 22)
        if self.line_segments:
            for segment_index, segment in enumerate(self.line_segments):
                try:
                    start_idx, end_idx = int(segment.get("from")), int(segment.get("to"))
                    if start_idx < 0 or end_idx < 0 or start_idx >= len(screen_points) or end_idx >= len(screen_points):
                        continue
                    style = Qt.DashLine if segment.get("style") == "dash" else Qt.SolidLine
                    if segment.get("blocked_reason") == "cross_group":
                        color = QColor(*PREVIEW_LINE_CROSS_GROUP)
                        style = Qt.DotLine
                        width = 2
                    else:
                        color = (
                            QColor(*PREVIEW_LINE_BRANCH)
                            if style == Qt.DashLine
                            else QColor(*PREVIEW_LINE_SOLID)
                        )
                        width = 2 if style == Qt.DashLine else 3
                    if segment_index in (
                        self.hover_segment_index,
                        self.drag_segment_index,
                    ):
                        if segment.get("blocked_reason") == "cross_group":
                            color = QColor(*PREVIEW_LINE_CROSS_GROUP_HOVER)
                        else:
                            color = QColor(*PREVIEW_LINE_ACTIVE)
                        width += 2
                    painter.setPen(QPen(color, width, style, Qt.RoundCap))
                    painter.drawLine(screen_points[start_idx], screen_points[end_idx])
                except Exception as exc:
                    logger.debug("绘制连线失败: %s", exc, exc_info=True)
                    continue
        elif self.draw_lines and len(screen_points) > 1:
            painter.setPen(QPen(QColor(0, 188, 212, 220), 3, Qt.SolidLine, Qt.RoundCap))
            for idx in range(len(screen_points) - 1):
                painter.drawLine(screen_points[idx], screen_points[idx + 1])

        painter.setFont(_overlay_font(10, bold=True))
        normalized_marked = set()
        for mark_idx in self.marked_indices:
            normalized_marked.add(len(screen_points) + mark_idx if mark_idx < 0 else mark_idx)
        for idx, point in enumerate(screen_points, 1):
            zero_idx = idx - 1
            if zero_idx in normalized_marked:
                fill = QColor(156, 39, 176, 135)
            elif idx == 1:
                fill = QColor(76, 175, 80, 135)
            elif idx == len(screen_points):
                fill = QColor(244, 67, 54, 135)
            else:
                fill = QColor(255, 193, 7, 125)

            radius = 7 if zero_idx in normalized_marked else (6 if zero_idx in normalized_editable else 5)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(255, 255, 255, 185), 2 if zero_idx in normalized_editable or zero_idx in normalized_marked else 1))
            painter.drawEllipse(point, radius, radius)
            painter.setBrush(QColor(255, 255, 255, 230))
            painter.setPen(QPen(QColor(0, 0, 0, 180), 1))
            painter.drawEllipse(point, 2, 2)

            if zero_idx == self.hover_index or zero_idx == self.drag_index:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(*INPUT_PANEL_BORDER), 3))
                painter.drawEllipse(point, 11, 11)

            label = self.point_labels[zero_idx] if zero_idx < len(self.point_labels) and self.point_labels[zero_idx] else str(idx)
            if zero_idx in normalized_marked and "*" not in label:
                label = f"{label}*"
            label_w = max(48, min(96, painter.fontMetrics().horizontalAdvance(label) + 16))
            label_rect = QRect(point.x() + 8, point.y() - int(20 * s), label_w, int(22 * s))
            painter.fillRect(label_rect, QColor(0, 0, 0, 155))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(label_rect, Qt.AlignCenter, label)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.close()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            idx = self.nearest_editable_index(pos)
            if idx is not None:
                self.drag_index = idx
                self._point_drag_original_points = list(self.points)
                self._last_drag_callback_ns = 0
                self.hover_segment_index = None
                self.setCursor(Qt.SizeAllCursor)
                event.accept()
                return
            segment_index = self.nearest_segment_index(
                pos,
                include_blocked=True,
            )
            if segment_index is not None:
                move_indices = self._segment_move_indices(segment_index)
                if move_indices:
                    self.drag_segment_index = segment_index
                    self._segment_drag_original_points = list(self.points)
                    self._segment_drag_start_input = self.from_screen_point(pos)
                    self.hover_index = None
                    self.setCursor(Qt.SizeAllCursor)
                else:
                    self.hover_segment_index = segment_index
                    self.setCursor(Qt.ForbiddenCursor)
                    self.update()
                event.accept()
                return
            if self.close_on_left_blank:
                self.close()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self.drag_index is not None:
            x, y = self.from_screen_point(pos)
            self.points[self.drag_index] = (x, y)
            self._screen_points_cache = None
            # Keep the dragged marker visually following the cursor on every
            # move (cheap), but only re-run the heavy update callback at a
            # throttled rate to avoid pegging the CPU during a drag.
            now = time.perf_counter_ns()
            if self._last_drag_callback_ns and (
                now - self._last_drag_callback_ns
            ) < DRAG_CALLBACK_THROTTLE_NS:
                self._schedule_update()
                event.accept()
                return
            self._last_drag_callback_ns = now
            if self.point_moved_callback:
                try:
                    updated_points = self.point_moved_callback(
                        self.drag_index, int(round(x)), int(round(y))
                    )
                    if updated_points:
                        self.set_points(updated_points)
                except RuntimeError:
                    self.point_moved_callback = None
            self._schedule_update()
            event.accept()
            return
        if self.drag_segment_index is not None:
            original = self._segment_drag_original_points
            start = self._segment_drag_start_input
            if original is None or start is None:
                self.drag_segment_index = None
                event.accept()
                return
            current = self.from_screen_point(pos)
            dx = current[0] - start[0]
            dy = current[1] - start[1]
            for index in self._segment_move_indices(self.drag_segment_index):
                self.points[index] = (
                    original[index][0] + dx,
                    original[index][1] + dy,
                )
            self._screen_points_cache = None
            self._schedule_update()
            event.accept()
            return

        now = time.perf_counter_ns()
        if self._last_hover_hit_test_ns and (
            now - self._last_hover_hit_test_ns
        ) < HOVER_HIT_TEST_THROTTLE_NS:
            event.accept()
            return
        self._last_hover_hit_test_ns = now
        hover_index = self.nearest_editable_index(pos)
        hover_segment_index = None
        if hover_index is None:
            hover_segment_index = self.nearest_segment_index(
                pos,
                include_blocked=True,
            )
        if (
            hover_index != self.hover_index
            or hover_segment_index != self.hover_segment_index
        ):
            self.hover_index = hover_index
            self.hover_segment_index = hover_segment_index
            self.update()
        if hover_index is not None:
            cursor = Qt.SizeAllCursor
        elif hover_segment_index is not None:
            cursor = (
                Qt.SizeAllCursor
                if self._segment_move_indices(hover_segment_index)
                else Qt.ForbiddenCursor
            )
        else:
            cursor = Qt.PointingHandCursor
        self.setCursor(cursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.drag_index is not None:
            idx = self.drag_index
            self.drag_index = None
            self._point_drag_original_points = None
            self._last_drag_callback_ns = 0
            self.hover_index = idx
            self.setCursor(Qt.PointingHandCursor)
            x, y = self.points[idx]
            release_cb = self.point_released_callback or self.point_moved_callback
            if release_cb:
                try:
                    updated_points = release_cb(idx, int(round(x)), int(round(y)))
                    if updated_points:
                        self.set_points(updated_points)
                except RuntimeError:
                    pass
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.drag_segment_index is not None:
            segment_index = self.drag_segment_index
            start = self._segment_drag_start_input
            current = self.from_screen_point(event.position().toPoint())
            dx = current[0] - start[0] if start is not None else 0.0
            dy = current[1] - start[1] if start is not None else 0.0
            self.drag_segment_index = None
            self._segment_drag_original_points = None
            self._segment_drag_start_input = None
            self.hover_segment_index = segment_index
            self.setCursor(Qt.PointingHandCursor)
            if self.line_released_callback and (round(dx) or round(dy)):
                try:
                    updated_points = self.line_released_callback(
                        segment_index,
                        int(round(dx)),
                        int(round(dy)),
                    )
                    if updated_points:
                        self.set_points(updated_points)
                except RuntimeError:
                    pass
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if (
            self.drag_index is None
            and self.drag_segment_index is None
            and (self.hover_index is not None or self.hover_segment_index is not None)
        ):
            self.hover_index = None
            self.hover_segment_index = None
            self.update()
        super().leaveEvent(event)

    def closeEvent(self, event):
        active = (
            type(self)._active_preview_ref()
            if callable(type(self)._active_preview_ref)
            else None
        )
        if active is self:
            type(self)._active_preview_ref = None
        self.point_moved_callback = None
        self.point_released_callback = None
        self.line_released_callback = None
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if (
                self.drag_segment_index is not None
                and self._segment_drag_original_points is not None
            ):
                self.set_points(self._segment_drag_original_points)
                self.drag_segment_index = None
                self._segment_drag_original_points = None
                self._segment_drag_start_input = None
                self.setCursor(Qt.PointingHandCursor)
                event.accept()
                return
            if (
                self.drag_index is not None
                and self.point_moved_callback is None
                and self._point_drag_original_points is not None
            ):
                self.set_points(self._point_drag_original_points)
                self.drag_index = None
                self._point_drag_original_points = None
                self.setCursor(Qt.PointingHandCursor)
                event.accept()
                return
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)


class RunningStatusOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.position_name = "右上角"
        self.activity_name = "脚本"
        self.start_time = time.time()
        self.status_data = {}
        screen = QApplication.primaryScreen()
        dpi_scale = screen.logicalDotsPerInch() / 96.0 if screen is not None else 1.0
        ui_scale = _ui_scale()
        # UI-CONVENTION: allow fixed-geometry reason=短暂顶层覆盖层需要稳定宽度约束自动换行，标签改为填满内容区
        self.setFixedWidth(int(280 * dpi_scale * ui_scale))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.bg = QFrame()
        self.bg.setStyleSheet(
            f"background-color: {OVERLAY_BG}; border-radius: 8px; border: 2px solid {COLOR_FOCUS};"
        )
        layout.addWidget(self.bg)

        inner = QVBoxLayout(self.bg)
        # UI-CONVENTION: allow layout-metrics reason=左右内边距与固定宽度、字号使用同一 DPI/界面倍率，保证留白对称
        inner.setContentsMargins(
            int(12 * dpi_scale * ui_scale),
            int(8 * dpi_scale * ui_scale),
            int(12 * dpi_scale * ui_scale),
            int(8 * dpi_scale * ui_scale),
        )
        self.label = QLabel()
        self.label.setStyleSheet(
            f"color: white; font-size: {int(13 * dpi_scale * ui_scale)}px; "
            f"font-weight: bold;"
        )
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        inner.addWidget(self.label)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_text)

    def start_overlay(self, position_name, activity_name="脚本"):
        self.position_name = position_name
        self.activity_name = str(activity_name or "脚本")
        self.start_time = time.time()
        self.status_data = {}
        self.timer.start(500)
        self.refresh_text()
        self.show()
        self.raise_()
        # FR-20261008-802: the v1.0.43 fix set WA_TransparentForMouseEvents only,
        # which is a Qt-internal attribute and does not become WS_EX_TRANSPARENT
        # on Windows, so the panel kept swallowing clicks in its rectangle. Add
        # the real click-through flag and re-apply it after show (some platform
        # plugins drop it while the top-level HWND is first created).
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        schedule_widget_callback(
            self, 0, lambda owner: exclude_window_from_capture(owner)
        )

    def set_status(self, data):
        self.status_data.update(data)
        self.refresh_text()

    def refresh_text(self):
        elapsed = int(time.time() - self.start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        elapsed_text = f"{h:02d}:{m:02d}:{s:02d}"
        cmd = str(self.status_data.get("cmd", "") or "")
        detail = str(self.status_data.get("detail", "") or "")
        if self.activity_name == "脚本":
            loop = self.status_data.get("loop", 1)
            step = self.status_data.get("step", 0)
            total = self.status_data.get("total", 0)
            step_text = f"步骤 {step}/{total}" if step and total else "准备中"
            if cmd:
                step_text += f" | {cmd}"
            body = f"第 {loop} 次 | {step_text}"
        else:
            body = cmd or "运行中"
            if detail:
                body += f" | {detail}"
        self.label.setText(
            f"{self.activity_name}正在执行中\n{body}\n已运行 {elapsed_text}"
        )
        self.adjustSize()
        self.move_to_position()

    def move_to_position(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        margin = 18
        x = max(rect.x() + margin, rect.x() + rect.width() - self.width() - margin)
        y = rect.y() + rect.height() - self.height() - margin if self.position_name == "右下角" else rect.y() + margin
        self.move(x, y)

    def stop_overlay(self):
        self.timer.stop()
        self.hide()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)

class MouseTrailOverlay(QWidget):
    """One click-through virtual-desktop surface for bounded fading trails."""

    MAX_POINTS = 64
    LIFETIME_SECONDS = 0.42
    FRAME_INTERVAL_MS = 33

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.screen_mapper = ScreenCoordinateMapper()
        self.screen_mapper.apply_to(self)
        self._points = []
        self._path_id = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance_fade)
        schedule_widget_callback(
            self, 0, lambda owner: exclude_window_from_capture(owner)
        )

    def add_points(self, points: object, *, new_path=False):
        """Append one bounded physical-coordinate batch and start repainting."""

        if new_path or not self._points:
            self._path_id += 1
        now = time.monotonic()
        if not isinstance(points, (list, tuple)):
            return
        for raw in points[:16]:
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                continue
            try:
                x, y = raw[0], raw[1]
                local = _input_to_local(self.screen_mapper, float(x), float(y))
            except (TypeError, ValueError):
                continue
            self._points.append((local, now, self._path_id))
        if len(self._points) > self.MAX_POINTS:
            self._points = self._points[-self.MAX_POINTS :]
        if not self._points:
            return
        if not self.isVisible():
            self.show()
            self.raise_()
            # FR-20261008-801: WA_TransparentForMouseEvents is a Qt-internal
            # attribute and does NOT map to WS_EX_TRANSPARENT on Windows, so a
            # top-level overlay still swallows clicks over its painted pixels
            # (the trail's last point sits exactly on the click target). The
            # WindowTransparentForInput flag is what makes the native window
            # click-through; some platform plugins drop it while the top-level
            # HWND is first created, so re-apply it after show like
            # ClickPointOverlay does.
            self.setWindowFlag(Qt.WindowTransparentForInput, True)
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _advance_fade(self):
        cutoff = time.monotonic() - self.LIFETIME_SECONDS
        self._points = [item for item in self._points if item[1] >= cutoff]
        if not self._points:
            self._timer.stop()
            self.hide()
            return
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()

        for previous, current in zip(self._points, self._points[1:], strict=False):
            if previous[2] != current[2]:
                continue
            fade = max(
                0.0,
                1.0 - (now - current[1]) / self.LIFETIME_SECONDS,
            )
            painter.setPen(
                QPen(
                    QColor(33, 150, 243, int(175 * fade)),
                    max(2, int(round(4 * _ui_scale()))),
                    Qt.SolidLine,
                    Qt.RoundCap,
                    Qt.RoundJoin,
                )
            )
            painter.drawLine(previous[0], current[0])

        radius = max(2, int(round(4 * _ui_scale())))
        painter.setPen(Qt.NoPen)
        for point, created_at, _path_id in self._points:
            fade = max(
                0.0,
                1.0 - (now - created_at) / self.LIFETIME_SECONDS,
            )
            painter.setBrush(QColor(76, 175, 255, int(210 * fade)))
            painter.drawEllipse(point, radius, radius)

    def closeEvent(self, event):
        self._timer.stop()
        self._points.clear()
        super().closeEvent(event)


class ClickPointOverlay(QWidget):
    def __init__(self, x, y, text="", duration_ms=650):
        super().__init__()
        self.x = float(x)
        self.y = float(y)
        self.text = str(text or "")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self.screen_mapper = ScreenCoordinateMapper()
        self._configure_local_geometry()
        schedule_close(self, max(200, int(duration_ms)))
        self.show()
        # Some Qt platform plugins drop WindowTransparentForInput while the
        # native top-level window is first created. Re-apply it after show so
        # the Windows hit-test contract is present on the actual HWND.
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.show()
        self.raise_()
        schedule_widget_callback(
            self, 0, lambda owner: exclude_window_from_capture(owner)
        )

    def nativeEvent(self, event_type, message):
        """Keep the top-level click indicator transparent to hit testing."""

        try:
            msg = wintypes.MSG.from_address(int(message))
            if int(msg.message) == WM_NCHITTEST:
                return True, HTTRANSPARENT
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        return super().nativeEvent(event_type, message)

    def _configure_local_geometry(self):
        mapped = _input_to_local(self.screen_mapper, self.x, self.y)
        global_point = QPoint(
            mapped.x() + self.screen_mapper.virtual_rect.x(),
            mapped.y() + self.screen_mapper.virtual_rect.y(),
        )
        screen = QApplication.screenAt(global_point) or QApplication.primaryScreen()
        bounds = screen.geometry() if screen else self.screen_mapper.virtual_rect
        width = 220 if self.text else 64
        height = 64
        cross_margin = 32
        prefer_right = global_point.x() + width - cross_margin <= bounds.right()
        left = (
            global_point.x() - cross_margin
            if prefer_right
            else global_point.x() - (width - cross_margin)
        )
        top = global_point.y() - cross_margin
        left = max(bounds.left(), min(left, bounds.right() - width + 1))
        top = max(bounds.top(), min(top, bounds.bottom() - height + 1))
        self.setGeometry(left, top, width, height)
        self._click_point = QPoint(global_point.x() - left, global_point.y() - top)
        # 钳制点击点到 overlay 本地范围内，避免 offscreen / 越界屏幕下几何
        # 计算导致点击点落在窗口外（真实屏幕下本就在范围内，无副作用）。
        self._click_point = QPoint(
            max(0, min(self._click_point.x(), width - 1)),
            max(0, min(self._click_point.y(), height - 1)),
        )
        self._label_on_left = self._click_point.x() > width // 2

    def to_screen_point(self):
        return QPoint(self._click_point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        point = self.to_screen_point()

        painter.setPen(QPen(QColor(255, 255, 255, 235), 4))
        painter.setBrush(QColor(33, 150, 243, 90))
        painter.drawEllipse(point, 18, 18)
        painter.setPen(QPen(QColor(244, 67, 54, 245), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(point.x() - 24, point.y(), point.x() + 24, point.y())
        painter.drawLine(point.x(), point.y() - 24, point.x(), point.y() + 24)

        if self.text:
            s = _ui_scale()
            painter.setFont(_overlay_font(10, bold=True))
            label = painter.fontMetrics().elidedText(self.text, Qt.ElideRight, 140)
            label_width = min(
                150, painter.fontMetrics().horizontalAdvance(label) + 20
            )
            if self._label_on_left:
                rect = QRect(point.x() - 24 - label_width, point.y() - int(14 * s), label_width, int(28 * s))
            else:
                rect = QRect(point.x() + 24, point.y() - int(14 * s), label_width, int(28 * s))
            painter.fillRect(rect, QColor(0, 0, 0, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect, Qt.AlignCenter, label)


class RecognitionMatchOverlay(QWidget):
    """Highlight one recognition result without sending any input."""

    def __init__(
        self,
        center_x,
        center_y,
        matched_width,
        matched_height,
        click_x,
        click_y,
        *,
        score=0.0,
        scale=1.0,
        duration_ms=5000,
    ):
        super().__init__()
        self.center_x = float(center_x)
        self.center_y = float(center_y)
        self.matched_width = max(2.0, float(matched_width))
        self.matched_height = max(2.0, float(matched_height))
        self.click_x = float(click_x)
        self.click_y = float(click_y)
        self.score = float(score)
        self.scale = float(scale)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)

        self.screen_mapper = ScreenCoordinateMapper()
        self.screen_mapper.apply_to(self)
        schedule_close(self, max(1000, int(duration_ms)))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _local_point(self, x, y):
        return _input_to_local(self.screen_mapper, x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        top_left = self._local_point(
            self.center_x - self.matched_width / 2.0,
            self.center_y - self.matched_height / 2.0,
        )
        bottom_right = self._local_point(
            self.center_x + self.matched_width / 2.0,
            self.center_y + self.matched_height / 2.0,
        )
        match_rect = QRect(top_left, bottom_right).normalized()
        match_rect.setWidth(max(2, match_rect.width()))
        match_rect.setHeight(max(2, match_rect.height()))

        painter.setBrush(QColor(255, 152, 0, 48))
        painter.setPen(QPen(QColor(255, 152, 0, 235), 3))
        painter.drawRect(match_rect)

        click_point = self._local_point(self.click_x, self.click_y)
        painter.setBrush(QColor(33, 150, 243, 85))
        painter.setPen(QPen(QColor(255, 255, 255, 235), 3))
        painter.drawEllipse(click_point, 10, 10)
        painter.setPen(QPen(QColor(244, 67, 54, 245), 2, Qt.SolidLine))
        painter.drawLine(
            click_point.x() - 15,
            click_point.y(),
            click_point.x() + 15,
            click_point.y(),
        )
        painter.drawLine(
            click_point.x(),
            click_point.y() - 15,
            click_point.x(),
            click_point.y() + 15,
        )

        painter.setFont(_overlay_font(10, bold=True))
        label = (
            f"测试命中  相似度 {self.score:.3f}  缩放 {self.scale:.3f}x  "
            "| 点击/右键/Esc 关闭"
        )
        label_width = min(
            max(330, painter.fontMetrics().horizontalAdvance(label) + 24),
            max(330, self.width() - 24),
        )
        label_x = max(
            12, min(match_rect.left(), self.width() - label_width - 12)
        )
        s = _ui_scale()
        label_y = match_rect.top() - int(38 * s)
        if label_y < 12:
            label_y = min(self.height() - int(42 * s), match_rect.bottom() + int(10 * s))
        label_rect = QRect(label_x, max(12, label_y), label_width, int(32 * s))
        painter.fillRect(label_rect, QColor(17, 24, 39, 225))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(label_rect, Qt.AlignCenter, label)

    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.RightButton):
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
