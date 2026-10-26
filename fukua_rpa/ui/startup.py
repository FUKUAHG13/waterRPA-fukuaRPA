# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Minimal first-frame window shown while the full workspace imports."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..constants import APP_VERSION, PRODUCT_NAME
from ..json_safety import loads_bounded_json
from ..paths import get_base_dir
from .theme import (
    STARTUP_BODY_DARK,
    STARTUP_BODY_LIGHT,
    STARTUP_BRAND_DARK,
    STARTUP_BRAND_LIGHT,
    STARTUP_CHUNK_DARK,
    STARTUP_CHUNK_LIGHT,
    STARTUP_STATUS_DARK,
    STARTUP_STATUS_LIGHT,
    STARTUP_TRACK_DARK,
    STARTUP_TRACK_LIGHT,
    STARTUP_VERSION_DARK,
    STARTUP_VERSION_LIGHT,
)


class StartupShell(QMainWindow):
    cancelled = Signal()
    first_painted = Signal(float)

    def __init__(self):
        super().__init__()
        self._handoff = False
        self._first_paint_emitted = False
        self.setWindowTitle(f"{PRODUCT_NAME} {APP_VERSION}")
        self.resize(760, 520)
        self.setMinimumSize(620, 420)

        body = QWidget()
        body.setObjectName("startupBody")
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(54, 46, 54, 46)
        layout.addStretch(3)

        brand = QLabel(PRODUCT_NAME)
        brand.setObjectName("startupBrand")
        brand.setAlignment(Qt.AlignCenter)
        layout.addWidget(brand)

        version = QLabel(APP_VERSION)
        version.setObjectName("startupVersion")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)
        layout.addSpacing(34)

        self.status_label = QLabel("正在准备工作区")
        self.status_label.setObjectName("startupStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        progress = QProgressBar()
        progress.setObjectName("startupProgress")
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(6)
        layout.addWidget(progress)
        layout.addStretch(4)

        # M-16: 读取用户主题模式，深色模式下使用深色启动样式避免闪烁。
        # 主题存储在 config.ini 的 profiles JSON 中，与主窗口使用相同配置路径。
        # NOTE: theme_mode is stored inside the profile config JSON. Parsing the
        # full JSON at startup is intentional — the bounded parser (1MB limit)
        # prevents abuse while keeping the single-source-of-truth design.
        theme_mode = "light"
        try:
            config_path = os.path.join(get_base_dir(), "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            current_profile = str(settings.value("current_profile", "默认方案"))
            raw = settings.value("profiles_commit_json") or settings.value(
                "profiles_json"
            )
            if raw:
                profiles_payload = loads_bounded_json(
                    raw, max_bytes=1024 * 1024, label="启动主题配置"
                )
                profiles = profiles_payload.get("profiles", profiles_payload)
                if isinstance(profiles, dict):
                    profile_cfg = profiles.get(current_profile, {})
                    if isinstance(profile_cfg, dict):
                        theme_mode = str(profile_cfg.get("theme_mode", "light"))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("启动画面主题读取失败，使用默认浅色: %s", exc)
            theme_mode = "light"

        if theme_mode == "dark":
            self.setStyleSheet(
                # FR-20260809-0701: 颜色来自 theme.py 语义 token（单一真相源），
                # 主题调整后启动画面自动跟随。
                f"QWidget#startupBody {{ background: {STARTUP_BODY_DARK}; }}"
                f"QLabel#startupBrand {{ color: {STARTUP_BRAND_DARK}; font-size: 34px; font-weight: 750; }}"
                f"QLabel#startupVersion {{ color: {STARTUP_VERSION_DARK}; font-size: 13px; }}"
                f"QLabel#startupStatus {{ color: {STARTUP_STATUS_DARK}; font-size: 14px; padding: 8px; }}"
                f"QProgressBar#startupProgress {{ background: {STARTUP_TRACK_DARK}; border: none; border-radius: 3px; }}"
                f"QProgressBar#startupProgress::chunk {{ background: {STARTUP_CHUNK_DARK}; border-radius: 3px; }}"
            )
        else:
            self.setStyleSheet(
                f"QWidget#startupBody {{ background: {STARTUP_BODY_LIGHT}; }}"
                f"QLabel#startupBrand {{ color: {STARTUP_BRAND_LIGHT}; font-size: 34px; font-weight: 750; }}"
                f"QLabel#startupVersion {{ color: {STARTUP_VERSION_LIGHT}; font-size: 13px; }}"
                f"QLabel#startupStatus {{ color: {STARTUP_STATUS_LIGHT}; font-size: 14px; padding: 8px; }}"
                f"QProgressBar#startupProgress {{ background: {STARTUP_TRACK_LIGHT}; border: none; border-radius: 3px; }}"
                f"QProgressBar#startupProgress::chunk {{ background: {STARTUP_CHUNK_LIGHT}; border-radius: 3px; }}"
            )

    def handoff(self):
        self._handoff = True
        self.close()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._first_paint_emitted:
            self._first_paint_emitted = True
            self.first_painted.emit(time.perf_counter())

    def closeEvent(self, event):
        if not self._handoff:
            self.cancelled.emit()
        super().closeEvent(event)
