# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Image crop dialog for quick template trimming from the gear menu."""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import tempfile

from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_CROP_BORDER_COLOR = "#aaa"
_CROP_BG_COLOR = "#f0f0f0"


class ImageCropCanvas(QWidget):
    """Render and select an image in one coordinate system."""

    selectionChanged = Signal(QRect)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 400)
        self.setCursor(Qt.CrossCursor)
        self._original_pixmap = QPixmap()
        self._scaled_pixmap = QPixmap()
        self._pixmap_rect = QRect()
        self._selection_rect = QRect()
        self._dragging = False
        self._start_pos = QPoint()

    @property
    def pixmap_rect(self) -> QRect:
        return QRect(self._pixmap_rect)

    @property
    def selection_rect(self) -> QRect:
        return QRect(self._selection_rect)

    def set_pixmap(self, pixmap: QPixmap):
        self._original_pixmap = QPixmap(pixmap)
        self._update_display_geometry(clear_selection=True)

    def clear_selection(self):
        if self._selection_rect.isNull() and not self._dragging:
            return
        self._selection_rect = QRect()
        self._dragging = False
        self.selectionChanged.emit(QRect())
        self.update()

    def set_selection_rect(self, rect: QRect):
        selection = rect.normalized().intersected(self._pixmap_rect)
        if selection == self._selection_rect:
            return
        self._selection_rect = selection
        self.selectionChanged.emit(self.source_crop_rect())
        self.update()

    def source_crop_rect(self) -> QRect:
        display = self._pixmap_rect
        selection = self._selection_rect.intersected(display)
        if (
            self._original_pixmap.isNull()
            or display.width() <= 0
            or display.height() <= 0
            or selection.width() <= 0
            or selection.height() <= 0
        ):
            return QRect()

        source_width = self._original_pixmap.width()
        source_height = self._original_pixmap.height()
        relative_left = selection.left() - display.left()
        relative_top = selection.top() - display.top()
        relative_right = relative_left + selection.width()
        relative_bottom = relative_top + selection.height()

        x0 = math.floor(relative_left * source_width / display.width())
        y0 = math.floor(relative_top * source_height / display.height())
        x1 = math.ceil(relative_right * source_width / display.width())
        y1 = math.ceil(relative_bottom * source_height / display.height())

        x0 = max(0, min(int(x0), source_width - 1))
        y0 = max(0, min(int(y0), source_height - 1))
        x1 = max(x0 + 1, min(int(x1), source_width))
        y1 = max(y0 + 1, min(int(y1), source_height))
        return QRect(x0, y0, x1 - x0, y1 - y0)

    def resizeEvent(self, event):
        # DESIGN: 缩放改变 _scaled_pixmap 与 _pixmap_rect 的几何关系，
        # 选区按显示坐标存储，缩放后坐标失效。清除选区避免错位，
        # 用户需重新框选。这是有意的产品取舍而非遗漏。
        super().resizeEvent(event)
        self._update_display_geometry(clear_selection=True)

    def _update_display_geometry(self, *, clear_selection=False):
        if clear_selection:
            self.clear_selection()
        if self._original_pixmap.isNull():
            self._scaled_pixmap = QPixmap()
            self._pixmap_rect = QRect()
            self.update()
            return

        available = self.contentsRect().adjusted(10, 10, -10, -10)
        if available.width() < 10 or available.height() < 10:
            self._scaled_pixmap = QPixmap()
            self._pixmap_rect = QRect()
            self.update()
            return
        self._scaled_pixmap = self._original_pixmap.scaled(
            available.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        left = available.left() + (
            available.width() - self._scaled_pixmap.width()
        ) // 2
        top = available.top() + (
            available.height() - self._scaled_pixmap.height()
        ) // 2
        self._pixmap_rect = QRect(
            left,
            top,
            self._scaled_pixmap.width(),
            self._scaled_pixmap.height(),
        )
        self.update()

    def _clamp_to_pixmap(self, point: QPoint) -> QPoint:
        return QPoint(
            max(self._pixmap_rect.left(), min(point.x(), self._pixmap_rect.right())),
            max(self._pixmap_rect.top(), min(point.y(), self._pixmap_rect.bottom())),
        )

    def mousePressEvent(self, event):
        point = event.position().toPoint()
        if event.button() == Qt.LeftButton and self._pixmap_rect.contains(point):
            self._dragging = True
            self._start_pos = point
            self.set_selection_rect(QRect(point, point))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.set_selection_rect(
                QRect(self._start_pos, self._clamp_to_pixmap(event.position().toPoint()))
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self.set_selection_rect(
                QRect(self._start_pos, self._clamp_to_pixmap(event.position().toPoint()))
            )
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_CROP_BG_COLOR))
        painter.setPen(QPen(QColor(_CROP_BORDER_COLOR), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if not self._scaled_pixmap.isNull():
            painter.drawPixmap(self._pixmap_rect.topLeft(), self._scaled_pixmap)

        if self._selection_rect.width() > 1 and self._selection_rect.height() > 1:
            outside = QRegion(self._pixmap_rect).subtracted(
                QRegion(self._selection_rect)
            )
            painter.save()
            painter.setClipRegion(outside)
            painter.fillRect(self._pixmap_rect, QColor(0, 0, 0, 100))
            painter.restore()
            painter.setPen(QPen(QColor(0, 120, 215), 2, Qt.DashLine))
            painter.drawRect(self._selection_rect)


class ImageCropDialog(QDialog):
    """Dialog to crop a region from a template image by dragging a rectangle."""

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.setWindowTitle("裁剪图片")
        self.setMinimumSize(640, 520)
        self.setModal(True)
        self._pixmap = QPixmap()
        self._setup_ui()
        self._load_image()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self._canvas = ImageCropCanvas(self)
        self._canvas.selectionChanged.connect(self._selection_changed)
        layout.addWidget(self._canvas, 1)

        hint = QLabel("在图片上拖拽鼠标框选裁剪区域，确认后覆盖原文件")
        hint.setAlignment(Qt.AlignCenter)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        btn_layout = QHBoxLayout()
        self._info_label = QLabel("")
        self._info_label.setProperty("role", "muted")
        btn_layout.addWidget(self._info_label)
        btn_layout.addStretch()

        self._crop_btn = QPushButton("确认裁剪")
        self._crop_btn.setEnabled(False)
        self._crop_btn.clicked.connect(self._do_crop)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._crop_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _load_image(self):
        self._pixmap = QPixmap(self.image_path)
        if self._pixmap.isNull():
            self.reject()
            return
        self._canvas.set_pixmap(self._pixmap)

    def _selection_changed(self, source_rect: QRect):
        display_rect = self._canvas.selection_rect
        valid = (
            display_rect.width() > 5
            and display_rect.height() > 5
            and source_rect.width() > 1
            and source_rect.height() > 1
        )
        self._crop_btn.setEnabled(valid)
        if valid:
            self._info_label.setText(
                f"选区: {source_rect.width()} × {source_rect.height()} 像素"
            )
        else:
            self._info_label.clear()

    def _do_crop(self):
        source_rect = self._canvas.source_crop_rect()
        if source_rect.width() < 2 or source_rect.height() < 2:
            return

        try:
            with Image.open(self.image_path) as image:
                source = image.copy()
            x = min(source_rect.x(), max(0, source.width - 1))
            y = min(source_rect.y(), max(0, source.height - 1))
            width = min(source_rect.width(), source.width - x)
            height = min(source_rect.height(), source.height - y)
            if width < 2 or height < 2:
                return
            cropped = source.crop((x, y, x + width, y + height))
            # 写入同目录临时文件，成功后原子替换，避免写入中断损坏原文件
            dir_name = os.path.dirname(os.path.abspath(self.image_path))
            fd, tmp_path = tempfile.mkstemp(
                suffix=os.path.splitext(self.image_path)[1],
                dir=dir_name,
            )
            os.close(fd)
            try:
                # 备份原文件（.bak 后缀）
                backup_path = self.image_path + ".bak"
                with contextlib.suppress(OSError):  # 备份失败不阻止裁剪
                    shutil.copy2(self.image_path, backup_path)
                cropped.save(tmp_path)
                os.replace(tmp_path, self.image_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", f"图片处理出错：{exc}")
            self.reject()
            return

        self.accept()
