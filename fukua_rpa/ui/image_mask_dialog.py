# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Editor for image-relative regions ignored during template matching."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..vision.core import normalize_template_mask_regions


class ImageMaskWidget(QWidget):
    regions_changed = Signal()

    def __init__(self, pixmap, regions=(), scale=1.0):
        super().__init__()
        self.pixmap = pixmap
        self.display_scale = max(0.2, float(scale))
        self._regions = list(normalize_template_mask_regions(regions))
        self._drag_start: QPointF | None = None
        self._drag_current: QPointF | None = None
        self.setFixedSize(
            max(1, int(self.pixmap.width() * self.display_scale)),
            max(1, int(self.pixmap.height() * self.display_scale)),
        )
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def normalized_regions(self):
        return list(normalize_template_mask_regions(self._regions))

    def clear_regions(self):
        if not self._regions:
            return
        self._regions.clear()
        self.regions_changed.emit()
        self.update()

    def undo_region(self):
        if not self._regions:
            return
        self._regions.pop()
        self.regions_changed.emit()
        self.update()

    def _display_rect(self, region):
        x, y, width, height = region
        return QRectF(
            x * self.width(),
            y * self.height(),
            width * self.width(),
            height * self.height(),
        )

    def _clamped_point(self, point):
        return QPointF(
            max(0.0, min(float(self.width()), float(point.x()))),
            max(0.0, min(float(self.height()), float(point.y()))),
        )

    def _current_drag_rect(self):
        if self._drag_start is None or self._drag_current is None:
            return QRectF()
        return QRectF(self._drag_start, self._drag_current).normalized()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(self.rect(), self.pixmap)

        painter.setRenderHint(QPainter.Antialiasing, False)
        for index, region in enumerate(self._regions, 1):
            rect = self._display_rect(region)
            painter.fillRect(rect, QColor(37, 99, 235, 86))
            painter.setPen(QPen(QColor(29, 78, 216), 2, Qt.SolidLine))
            painter.drawRect(rect)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect.adjusted(6, 4, -4, -4), Qt.AlignLeft | Qt.AlignTop, str(index))

        drag_rect = self._current_drag_rect()
        if drag_rect.isValid() and not drag_rect.isEmpty():
            painter.fillRect(drag_rect, QColor(59, 130, 246, 64))
            painter.setPen(QPen(QColor(37, 99, 235), 2, Qt.DashLine))
            painter.drawRect(drag_rect)

    def mousePressEvent(self, event):
        point = self._clamped_point(event.position())
        if event.button() == Qt.LeftButton:
            self._drag_start = point
            self._drag_current = point
            self.update()
            event.accept()
            return
        if event.button() == Qt.RightButton:
            for index in range(len(self._regions) - 1, -1, -1):
                if self._display_rect(self._regions[index]).contains(point):
                    self._regions.pop(index)
                    self.regions_changed.emit()
                    self.update()
                    break
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None:
            self._drag_current = self._clamped_point(event.position())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_start is not None:
            self._drag_current = self._clamped_point(event.position())
            rect = self._current_drag_rect()
            self._drag_start = None
            self._drag_current = None
            if rect.width() >= 3 and rect.height() >= 3:
                self._regions.append(
                    (
                        rect.left() / max(1, self.width()),
                        rect.top() / max(1, self.height()),
                        rect.width() / max(1, self.width()),
                        rect.height() / max(1, self.height()),
                    )
                )
                self._regions = list(normalize_template_mask_regions(self._regions))
                self.regions_changed.emit()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImageMaskDialog(QDialog):
    def __init__(self, image_path, regions=(), parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap(image_path)
        if self.pixmap.isNull():
            raise ValueError("图片无法打开或格式不受支持")

        self.setWindowTitle("忽略图片局部变化")
        self.setMinimumSize(640, 480)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "按住左键框选会变化、无需参与识别的区域；可框选多处。"
            "右键点击蓝色区域可删除该区域。软件仍会在屏幕上寻找整张图片，并按整图位置点击。"
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        screen = QApplication.primaryScreen()
        screen_rect = QRect(0, 0, 1920, 1080) if screen is None else screen.availableGeometry()
        max_width = max(460, int(screen_rect.width() * 0.78))
        max_height = max(320, int(screen_rect.height() * 0.68))
        fit_scale = min(
            max_width / max(1, self.pixmap.width()),
            max_height / max(1, self.pixmap.height()),
        )
        longest = max(self.pixmap.width(), self.pixmap.height())
        preferred_scale = 4.0 if longest <= 240 else 2.0 if longest <= 700 else 1.0
        display_scale = max(0.2, min(preferred_scale, fit_scale))

        self.image_widget = ImageMaskWidget(
            self.pixmap,
            regions=regions,
            scale=display_scale,
        )
        self.image_widget.regions_changed.connect(self.update_summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setWidget(self.image_widget)
        layout.addWidget(scroll, 1)

        action_row = QHBoxLayout()
        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "sectionTitle")
        action_row.addWidget(self.summary_label, 1)
        undo_button = QPushButton("撤销上一区域")
        undo_button.clicked.connect(self.image_widget.undo_region)
        clear_button = QPushButton("清除全部")
        clear_button.clicked.connect(self.image_widget.clear_regions)
        action_row.addWidget(undo_button)
        action_row.addWidget(clear_button)
        layout.addLayout(action_row)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setProperty("role", "statusWarn")
        layout.addWidget(self.warning_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存忽略区域")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_summary()

        self.resize(
            min(max_width + 40, self.image_widget.width() + 60),
            min(max_height + 160, self.image_widget.height() + 190),
        )

    def update_summary(self):
        regions = self.image_widget.normalized_regions()
        ignored_area = min(1.0, sum(region[2] * region[3] for region in regions))
        self.summary_label.setText(
            f"已忽略 {len(regions)} 个区域；矩形面积合计约占图片 {ignored_area * 100:.1f}%"
        )
        self.warning_label.setText(
            "忽略区域较大。请确保剩余部分仍包含足够独特的边缘和纹理，否则可能误识别。"
            if ignored_area >= 0.65
            else ""
        )
        self.warning_label.setVisible(bool(self.warning_label.text()))

    def selected_regions(self):
        return self.image_widget.normalized_regions()
