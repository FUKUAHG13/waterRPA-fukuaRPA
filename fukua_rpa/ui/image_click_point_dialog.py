# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Image-relative click-point editor used by per-step settings."""

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class ImageClickPointWidget(QWidget):
    point_changed = Signal(float, float)

    def __init__(self, pixmap, rx=0.5, ry=0.5, scale=2.0, selectable=True):
        super().__init__()
        self.pixmap = pixmap
        self.rx = max(0.0, min(1.0, float(rx)))
        self.ry = max(0.0, min(1.0, float(ry)))
        self.selectable = selectable
        self.display_scale = max(0.2, float(scale))
        self.setFixedSize(
            max(1, int(self.pixmap.width() * self.display_scale)),
            max(1, int(self.pixmap.height() * self.display_scale)),
        )
        self.setCursor(Qt.CrossCursor if selectable else Qt.ArrowCursor)

    def set_point(self, rx, ry):
        self.rx = max(0.0, min(1.0, float(rx)))
        self.ry = max(0.0, min(1.0, float(ry)))
        self.point_changed.emit(self.rx, self.ry)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(self.rect(), self.pixmap)

        px = int(self.rx * self.width())
        py = int(self.ry * self.height())
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(0, 0, 0, 210), 5, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(px - 18, py, px + 18, py)
        painter.drawLine(px, py - 18, px, py + 18)
        painter.setPen(QPen(QColor(255, 0, 0), 2, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(px - 18, py, px + 18, py)
        painter.drawLine(px, py - 18, px, py + 18)
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawEllipse(QPoint(px, py), 5, 5)

    def mousePressEvent(self, event):
        if self.selectable and event.button() == Qt.LeftButton:
            pos = event.position() if hasattr(event, "position") else event.pos()
            self.set_point(
                pos.x() / max(1, self.width()), pos.y() / max(1, self.height())
            )
            event.accept()
            return
        super().mousePressEvent(event)


class ImageClickPointDialog(QDialog):
    def __init__(self, image_path, rx=0.5, ry=0.5, selectable=True, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.selectable = selectable
        self.pixmap = QPixmap(image_path)
        if self.pixmap.isNull():
            raise ValueError("图片无法打开或格式不受支持")

        self.setWindowTitle(
            "选择图片内点击位置" if selectable else "预览图片内点击位置"
        )
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)

        hint = (
            "左键点击放大图片中的目标位置；保存的是相对位置，缩放识别后仍会点同一处。"
        )
        if not selectable:
            hint = "红色十字即当前将点击的图片内相对位置。"
        hint_label = QLabel(hint)
        hint_label.setProperty("role", "muted")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        screen = QApplication.primaryScreen()
        screen_rect = QRect(0, 0, 1920, 1080) if screen is None else screen.availableGeometry()
        max_w = max(360, int(screen_rect.width() * 0.78))
        max_h = max(280, int(screen_rect.height() * 0.68))
        fit_scale = min(
            max_w / max(1, self.pixmap.width()), max_h / max(1, self.pixmap.height())
        )
        longest = max(self.pixmap.width(), self.pixmap.height())
        if longest <= 120:
            preferred_scale = 6.0
        elif longest <= 300:
            preferred_scale = 4.0
        elif longest <= 700:
            preferred_scale = 2.0
        else:
            preferred_scale = 1.0
        display_scale = max(0.2, min(preferred_scale, fit_scale))

        self.image_widget = ImageClickPointWidget(
            self.pixmap, rx, ry, display_scale, selectable
        )
        self.image_widget.point_changed.connect(self.update_info)

        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setWidget(self.image_widget)
        layout.addWidget(scroll, 1)

        self.info_label = QLabel()
        self.info_label.setProperty("role", "sectionTitle")
        layout.addWidget(self.info_label)
        self.update_info(self.image_widget.rx, self.image_widget.ry)

        buttons = (
            QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            if selectable
            else QDialogButtonBox(QDialogButtonBox.Close)
        )
        if selectable:
            buttons.button(QDialogButtonBox.Ok).setText("使用此位置")
            buttons.button(QDialogButtonBox.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
        else:
            buttons.rejected.connect(self.reject)
            buttons.button(QDialogButtonBox.Close).setText("关闭")
            buttons.button(QDialogButtonBox.Close).clicked.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(
            min(max_w + 40, self.image_widget.width() + 60),
            min(max_h + 120, self.image_widget.height() + 150),
        )

    def update_info(self, rx, ry):
        img_x = int(round(rx * self.pixmap.width()))
        img_y = int(round(ry * self.pixmap.height()))
        self.info_label.setText(
            f"当前相对位置：X {rx:.3f} / Y {ry:.3f}；原图像素约 ({img_x}, {img_y})"
        )

    def selected_ratio(self):
        return self.image_widget.rx, self.image_widget.ry
