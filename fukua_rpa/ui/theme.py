# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Shared visual system for the desktop interface without external assets."""

import logging
import math
import re
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLayout,
    QMainWindow,
    QPushButton,
    QWidget,
)
from shiboken6 import isValid

logger = logging.getLogger(__name__)

# Semantic color tokens reused by floating overlays/previews so the palette
# stays single-sourced. ARCHITECTURE.md invariant #7 requires UI colors to come
# from theme.py instead of being hard-coded next to a widget.
COLOR_PRIMARY = "#2563EB"
COLOR_FOCUS = "#3B82F6"
COLOR_DANGER = "#EF4444"
OVERLAY_BG = "rgba(17, 24, 39, 240)"
OVERLAY_BG_DANGER = "rgba(48, 20, 22, 240)"
# 计划任务表状态红（profile missing / invalid / 上次运行失败），与危险红区分。
COLOR_STATUS_ERROR = "#C0392B"
# 操作提示面板（input_tools 浮层）RGBA 元组，供 QColor(*token) 直接构造。
INPUT_PANEL_SHADOW = (15, 23, 42, 80)
INPUT_PANEL_BG = (250, 252, 255, 246)
INPUT_PANEL_BORDER = (37, 99, 235, 235)  # 与 COLOR_PRIMARY 同源
INPUT_PANEL_ACCENT = (37, 99, 235)       # 与 COLOR_PRIMARY 同源
PREVIEW_LINE_SOLID = (0, 188, 212, 220)
PREVIEW_LINE_BRANCH = (255, 193, 7, 175)
PREVIEW_LINE_CROSS_GROUP = (148, 163, 184, 145)
PREVIEW_LINE_CROSS_GROUP_HOVER = (100, 116, 139, 210)
PREVIEW_LINE_ACTIVE = (37, 99, 235, 240)
INPUT_PANEL_TITLE = (23, 32, 51)
INPUT_PANEL_BODY = (55, 65, 81)
INPUT_PANEL_MUTED = (79, 93, 117)

# Startup-splash semantic colors (FR-20260809-0701): the splash screen renders
# before the main theme applies, but its palette must still be single-sourced
# here so a theme change propagates instead of leaving hard-coded hex in
# ui/startup.py. _DARK/_LIGHT suffix follows the startup's own theme_mode.
STARTUP_BODY_DARK = "#1a1a2e"
STARTUP_BRAND_DARK = "#E0E0E0"
STARTUP_VERSION_DARK = "#9CA3AF"
STARTUP_STATUS_DARK = "#D1D5DB"
STARTUP_TRACK_DARK = "#374151"
STARTUP_CHUNK_DARK = COLOR_FOCUS  # "#3B82F6"
STARTUP_BODY_LIGHT = "#F7F9FC"
STARTUP_BRAND_LIGHT = "#172033"
STARTUP_VERSION_LIGHT = "#667085"
STARTUP_STATUS_LIGHT = "#475467"
STARTUP_TRACK_LIGHT = "#E4E7EC"
STARTUP_CHUNK_LIGHT = COLOR_PRIMARY  # "#2563EB"


APP_STYLESHEET = r"""
QWidget {
    color: #172033;
    font-family: "Microsoft YaHei UI", "Segoe UI";
}
QMainWindow, QDialog {
    background: #F4F6F8;
}
QWidget#appRoot, QWidget#settingsBody, QWidget#dialogBody {
    background: #F4F6F8;
}
QFrame#profileBar, QFrame#commandBar, QFrame#runBar, QFrame#statusBar {
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    border-radius: 7px;
}
QFrame#workspacePanel {
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    border-radius: 7px;
}
QFrame#onboardingNotice {
    background: #EEF5FF;
    border: 1px solid #AFC9EE;
    border-radius: 6px;
}
QFrame#onboardingNotice QLabel {
    color: #234E87;
}
QLabel[role="title"] {
    color: #101828;
    font-size: 12pt;
    font-weight: 700;
}
QLabel[role="sectionTitle"] {
    color: #344054;
    font-size: 10pt;
    font-weight: 650;
}
QLabel[role="muted"] {
    color: #667085;
}
QLabel[role="statusGood"] {
    color: #137A42;
    font-weight: 600;
}
QLabel[role="statusInfo"] {
    color: #2459A9;
    font-weight: 600;
}
QLabel[role="statusError"] {
    color: #B42318;
    font-weight: 700;
}
QLabel#autoClickerBanner {
    border: 1px solid #FFC107;
    border-radius: 6px;
    padding: 14px;
}
QLabel#autoClickerBanner[bannerState="idle"] {
    background: #FFF3CD;
    border-color: #FFC107;
}
QLabel#autoClickerBanner[bannerState="running"] {
    background: #D4EDDA;
    border-color: #28A745;
}
QLabel#autoClickerBanner[bannerState="error"] {
    background: #F8D7DA;
    border-color: #DC3545;
}
QLabel#autoClickerStatus {
    color: #666666;
    font-size: 9pt;
}
QLabel#manualMark {
    color: #FFFFFF;
    background: #7C3AED;
    border: none;
    border-radius: 4px;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#breakpointMark {
    color: #FFFFFF;
    background: #C9362B;
    border: none;
    border-radius: 4px;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#disabledMark {
    color: #FFFFFF;
    background: #667085;
    border: none;
    border-radius: 4px;
    font-size: 8pt;
    font-weight: 700;
}
QPushButton {
    min-height: 28px;
    padding: 0 12px;
    color: #344054;
    background: #FFFFFF;
    border: 1px solid #C9D0DA;
    border-radius: 5px;
    font-weight: 550;
}
QPushButton:hover {
    color: #1D4ED8;
    background: #F7FAFF;
    border-color: #7EA6E8;
}
QPushButton:pressed {
    background: #EAF1FD;
    border-color: #3B73C8;
}
QPushButton:focus {
    border: 1px solid #3B82F6;
}
QPushButton:disabled {
    color: #98A2B3;
    background: #F2F4F7;
    border-color: #E2E6EC;
}
QPushButton[variant="primary"] {
    color: #FFFFFF;
    background: #2563EB;
    border-color: #2563EB;
    font-weight: 650;
}
QPushButton[variant="primary"]:hover {
    background: #1D4ED8;
    border-color: #1D4ED8;
}
QPushButton[variant="tonal"] {
    color: #1E4F91;
    background: #EAF2FF;
    border-color: #B8CFF2;
}
QPushButton[variant="tonal"]:hover {
    background: #DCEAFF;
    border-color: #8FB2E8;
}
QPushButton[variant="success"] {
    color: #FFFFFF;
    background: #16803C;
    border-color: #16803C;
    font-weight: 700;
}
QPushButton[variant="success"]:hover {
    background: #116A32;
    border-color: #116A32;
}
QPushButton[variant="danger"] {
    color: #FFFFFF;
    background: #C9362B;
    border-color: #C9362B;
    font-weight: 700;
}
QPushButton[variant="danger"]:hover {
    background: #AA2D25;
    border-color: #AA2D25;
}
QPushButton[variant="ghost"] {
    color: #475467;
    background: transparent;
    border-color: transparent;
}
QPushButton[variant="ghost"]:hover {
    color: #1D4ED8;
    background: #EEF4FF;
    border-color: #D6E4FA;
}
QPushButton[variant="dangerGhost"] {
    color: #B42318;
    background: transparent;
    border-color: transparent;
}
QPushButton[variant="dangerGhost"]:hover {
    color: #8F2017;
    background: #FEECEB;
    border-color: #FACBC7;
}
QPushButton[variant="dangerGhost"][syntheticHover="true"] {
    color: #8F2017;
    background: #FEECEB;
    border-color: #FACBC7;
}
QPushButton#autoClickerAdvancedToggle {
    color: #0066CC;
    background: transparent;
    border: none;
}
QPushButton[bound="true"] {
    color: #137A42;
    background: #ECF8F0;
    border-color: #A9D8B8;
    font-weight: 650;
}
QPushButton[iconOnly="true"] {
    min-width: 30px;
    max-width: 30px;
    min-height: 28px;
    max-height: 28px;
    padding: 0;
}
QPushButton[compactText="true"] {
    min-width: 30px;
    padding: 0 6px;
}
QPushButton#helpButton {
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    padding: 0;
    color: #2459A9;
    background: #EAF2FF;
    border: 1px solid #B8CFF2;
    border-radius: 10px;
    font-size: 8pt;
    font-weight: 700;
}
QPushButton#helpButton:hover {
    color: #FFFFFF;
    background: #2563EB;
    border-color: #2563EB;
}
QFrame#helpPopup {
    color: #26364D;
    background: #FFFFFF;
    border: 1px solid #AFC5E8;
    border-radius: 6px;
}
QLabel#helpPopupText {
    color: #26364D;
    background: transparent;
    border: none;
}
QPushButton#sectionToggle {
    min-height: 34px;
    padding: 0 12px;
    text-align: left;
    color: #344054;
    background: #F7F9FC;
    border: 1px solid #D7DCE3;
    border-radius: 6px;
    font-weight: 650;
}
QPushButton#sectionToggle:hover {
    color: #1E4F91;
    background: #F0F5FD;
    border-color: #AFC5E8;
}
QPushButton#sectionToggle:checked {
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
}
QFrame#sectionContent {
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    border-top: none;
    border-bottom-left-radius: 6px;
    border-bottom-right-radius: 6px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    min-height: 28px;
    padding: 0 8px;
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #C9D0DA;
    border-radius: 5px;
    selection-background-color: #BFD4F6;
    selection-color: #172033;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #8FA4BF;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #3B82F6;
    background: #FFFFFF;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #98A2B3;
    background: #F2F4F7;
    border-color: #E2E6EC;
}
QLineEdit[generatedSummary="true"] {
    color: #667085;
    background: #F2F4F7;
    border-color: #E2E6EC;
}
QLineEdit[unsupportedAction="true"], QComboBox[unsupportedAction="true"] {
    color: #9B2C2C;
    background: #FFF4F2;
    border-color: #E7A39A;
}
QComboBox {
    padding-right: 24px;
}
QComboBox::drop-down {
    width: 22px;
    border: none;
}
QComboBox QAbstractItemView {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #C9D0DA;
    selection-color: #172033;
    selection-background-color: #DCEAFF;
    outline: 0;
}
QCheckBox {
    min-height: 28px;
    spacing: 7px;
    color: #344054;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
}
QCheckBox:disabled {
    color: #98A2B3;
}
QCheckBox[emphasis="true"] {
    color: #1E4F91;
    font-weight: 650;
}
QWidget[responsiveGroup="true"] {
    min-height: 30px;
}
QWidget[responsiveGroup="true"] QLabel,
QWidget[responsiveGroup="true"] QCheckBox {
    min-height: 28px;
}
QTextEdit, QPlainTextEdit, QListWidget {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    border-radius: 5px;
    selection-background-color: #DCEAFF;
    selection-color: #172033;
    outline: 0;
}
QListWidget#taskList {
    background: #F8FAFC;
    border: none;
    border-radius: 0;
    padding: 5px;
}
QListWidget#taskList::item {
    margin: 2px 0;
}
QListWidget#taskList::item:selected {
    background: transparent;
}
QPlainTextEdit#logView {
    color: #344054;
    background: #F8FAFC;
    border-color: #D7DCE3;
    font-family: "Cascadia Mono", "Microsoft YaHei UI";
    font-size: 9pt;
    padding: 7px;
}
QFrame#taskRow {
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    border-radius: 6px;
}
QFrame#taskRow[stepDisabled="true"] {
    background: #F2F4F7;
    border: 1px solid #98A2B3;
}
QFrame#taskRow[markerColor="red"] { background: #FEF2F2; border-left: 5px solid #EF4444; }
QFrame#taskRow[markerColor="orange"] { background: #FFF7ED; border-left: 5px solid #F59E0B; }
QFrame#taskRow[markerColor="yellow"] { background: #FEFCE8; border-left: 5px solid #EAB308; }
QFrame#taskRow[markerColor="green"] { background: #F0FDF4; border-left: 5px solid #22C55E; }
QFrame#taskRow[markerColor="blue"] { background: #EFF6FF; border-left: 5px solid #3B82F6; }
QFrame#taskRow[markerColor="purple"] { background: #FAF5FF; border-left: 5px solid #A855F7; }
QFrame#taskRow[markerColor="gray"] { background: #F2F4F7; border-left: 5px solid #98A2B3; }
QFrame#taskRow[editorHidden="true"] {
    color: #98A2B3;
    background: #F6F7F9;
    border-style: dashed;
}
QFrame#taskGroupHeader {
    background: #EAF2FF;
    border: none;
    border-bottom: 1px solid #B8D0F6;
}
QFrame#taskGroupHeader[groupDepth="2"] { background: #F0F5FC; }
QFrame#taskGroupHeader[groupDepth="3"] { background: #F5F7FB; }
QFrame#taskGroupHeader QLabel[role="groupTitle"] {
    color: #174EA6;
    font-weight: 700;
}
QFrame#taskRow[groupDepth="1"] { border-left: 3px solid #93B4F5; }
QFrame#taskRow[groupDepth="2"] { border-left: 5px solid #6FA0EC; }
QFrame#taskRow[groupDepth="3"] { border-left: 7px solid #4E86DD; }
QFrame#taskRow[groupStart="true"] { border-top-color: #6FA0EC; }
QFrame#taskRow[groupEnd="true"] { border-bottom-color: #6FA0EC; }
QFrame#taskRow[selected="true"] {
    background: #EFF5FF;
    border-top: 2px solid #3B82F6;
    border-right: 2px solid #3B82F6;
    border-bottom: 2px solid #3B82F6;
}
QFrame#taskRow[selected="true"][markerColor=""] {
    border: 2px solid #3B82F6;
}
QFrame#taskRow[dragSource="true"] {
    background: #E7F0FF;
    border: 2px solid #2563EB;
}
QLabel#taskDragSourceBadge {
    color: #174EA6;
    background: rgba(239, 245, 255, 242);
    border: 1px solid #93B4F5;
    border-radius: 4px;
    padding: 5px 9px;
    font-weight: 700;
}
QFrame#taskRow[debugPaused="true"] {
    background: #FFF7E8;
    border: 2px solid #D97706;
}
QFrame#taskRow QLabel#stepIndex {
    color: #667085;
    font-weight: 700;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    width: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:vertical {
    min-height: 28px;
    background: #BCC5D2;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover {
    background: #8F9BAD;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    height: 0;
    background: transparent;
}
QScrollBar:horizontal {
    height: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:horizontal {
    min-width: 28px;
    background: #BCC5D2;
    border-radius: 4px;
}
QScrollBar::handle:horizontal:hover {
    background: #8F9BAD;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    width: 0;
    background: transparent;
}
QSplitter::handle {
    background: #E4E7EC;
}
QSplitter::handle:hover {
    background: #AFC5E8;
}
QSplitter::handle:vertical {
    height: 5px;
}
QGroupBox {
    margin-top: 12px;
    padding-top: 8px;
    border: 1px solid #D7DCE3;
    border-radius: 6px;
    font-weight: 650;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: #344054;
    background: #FFFFFF;
}
QMenu {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #D7DCE3;
    padding: 4px;
}
QMenu::item {
    min-height: 26px;
    padding: 2px 22px 2px 10px;
    border-radius: 4px;
}
QMenu::item:selected {
    color: #172033;
    background: #DCEAFF;
}
QToolTip {
    color: #FFFFFF;
    background: #263241;
    border: 1px solid #465568;
    border-radius: 4px;
    padding: 6px 8px;
}
"""


DARK_STYLESHEET = r"""
QWidget {
    color: #E6EAF0;
}
QMainWindow, QDialog,
QWidget#appRoot, QWidget#settingsBody, QWidget#dialogBody {
    background: #111318;
}
QFrame#profileBar, QFrame#commandBar, QFrame#runBar, QFrame#statusBar,
QFrame#workspacePanel {
    background: #1B1F27;
    border-color: #3B4351;
}
QFrame#onboardingNotice {
    background: #18283D;
    border-color: #3C5D86;
}
QFrame#onboardingNotice QLabel {
    color: #A4C7FA;
}
QLabel[role="title"] {
    color: #F5F7FA;
}
QLabel[role="sectionTitle"] {
    color: #D6DCE6;
}
QLabel[role="muted"] {
    color: #9EA8B8;
}
QLabel[role="statusGood"] {
    color: #66D995;
}
QLabel[role="statusInfo"] {
    color: #8AB8FF;
}
QLabel[role="statusError"], QLabel[role="statusWarn"] {
    color: #FF8A82;
}
QLabel#manualMark, QLabel#breakpointMark, QLabel#disabledMark {
    color: #FFFFFF;
}
QPushButton {
    color: #D6DCE6;
    background: #1B1F27;
    border-color: #4A5565;
}
QPushButton:hover {
    color: #A8CBFF;
    background: #222A38;
    border-color: #6C9EEB;
}
QPushButton:pressed {
    background: #263A56;
    border-color: #477FD2;
}
QPushButton:focus {
    border-color: #5C9AFF;
}
QPushButton:disabled {
    color: #727C8C;
    background: #252A33;
    border-color: #343B46;
}
QPushButton[variant="primary"] {
    color: #FFFFFF;
    background: #3478F6;
    border-color: #3478F6;
}
QPushButton[variant="primary"]:hover {
    color: #FFFFFF;
    background: #4C8DFF;
    border-color: #4C8DFF;
}
QPushButton[variant="tonal"] {
    color: #B8D4FF;
    background: #1C2D46;
    border-color: #3F5F8C;
}
QPushButton[variant="tonal"]:hover {
    color: #D6E7FF;
    background: #243B5B;
    border-color: #638ECE;
}
QPushButton[variant="success"] {
    color: #FFFFFF;
    background: #237A45;
    border-color: #237A45;
}
QPushButton[variant="success"]:hover {
    color: #FFFFFF;
    background: #2D9655;
    border-color: #2D9655;
}
QPushButton[variant="danger"] {
    color: #FFFFFF;
    background: #B83B34;
    border-color: #B83B34;
}
QPushButton[variant="danger"]:hover {
    color: #FFFFFF;
    background: #D04A42;
    border-color: #D04A42;
}
QPushButton[variant="ghost"] {
    color: #B7C0CE;
    background: transparent;
    border-color: transparent;
}
QPushButton[variant="ghost"]:hover {
    color: #A8CBFF;
    background: #202F45;
    border-color: #344C70;
}
QPushButton[variant="dangerGhost"] {
    color: #FF9A93;
    background: transparent;
    border-color: transparent;
}
QPushButton[variant="dangerGhost"]:hover,
QPushButton[variant="dangerGhost"][syntheticHover="true"] {
    color: #FFC0BB;
    background: #402321;
    border-color: #6F3733;
}
QPushButton[bound="true"] {
    color: #78DDA0;
    background: #173426;
    border-color: #357A50;
}
QPushButton#helpButton {
    color: #A8CBFF;
    background: #1C2D46;
    border-color: #3F5F8C;
}
QPushButton#helpButton:hover {
    color: #FFFFFF;
    background: #3478F6;
    border-color: #3478F6;
}
QFrame#helpPopup {
    color: #E6EAF0;
    background: #202631;
    border: 1px solid #4F6685;
    border-radius: 6px;
}
QLabel#helpPopupText {
    color: #E6EAF0;
    background: transparent;
    border: none;
}
QPushButton#sectionToggle {
    color: #D6DCE6;
    background: #202631;
    border-color: #3B4351;
}
QPushButton#sectionToggle:hover {
    color: #B8D4FF;
    background: #212C3D;
    border-color: #405F84;
}
QFrame#sectionContent {
    background: #1B1F27;
    border-color: #3B4351;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    color: #E6EAF0;
    background: #1B1F27;
    border-color: #4A5565;
    selection-background-color: #34547E;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #718098;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    background: #1B1F27;
    border-color: #5C9AFF;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QLineEdit[generatedSummary="true"] {
    color: #727C8C;
    background: #252A33;
    border-color: #343B46;
}
QLineEdit[unsupportedAction="true"], QComboBox[unsupportedAction="true"] {
    color: #FFAAA4;
    background: #3A2423;
    border-color: #8C4B46;
}
QComboBox QAbstractItemView {
    color: #E6EAF0;
    background: #1B1F27;
    border-color: #4A5565;
    selection-color: #FFFFFF;
    selection-background-color: #34547E;
}
QCheckBox {
    color: #D6DCE6;
}
QCheckBox:disabled {
    color: #727C8C;
}
QCheckBox[emphasis="true"] {
    color: #B8D4FF;
}
QTextEdit, QPlainTextEdit, QListWidget {
    color: #E6EAF0;
    background: #1B1F27;
    border-color: #3B4351;
    selection-background-color: #34547E;
    selection-color: #FFFFFF;
}
QListWidget#taskList, QPlainTextEdit#logView {
    color: #D6DCE6;
    background: #161A22;
    border-color: #3B4351;
}
QFrame#taskRow {
    background: #1B1F27;
    border-color: #3B4351;
}
QFrame#taskRow[stepDisabled="true"] {
    background: #252A33;
    border-color: #727C8C;
}
QFrame#taskRow[markerColor="red"] { background: #352226; border-left: 5px solid #F87171; }
QFrame#taskRow[markerColor="orange"] { background: #352A1E; border-left: 5px solid #FBBF24; }
QFrame#taskRow[markerColor="yellow"] { background: #33311D; border-left: 5px solid #FACC15; }
QFrame#taskRow[markerColor="green"] { background: #203229; border-left: 5px solid #4ADE80; }
QFrame#taskRow[markerColor="blue"] { background: #1E304A; border-left: 5px solid #60A5FA; }
QFrame#taskRow[markerColor="purple"] { background: #30243A; border-left: 5px solid #C084FC; }
QFrame#taskRow[markerColor="gray"] { background: #282D36; border-left: 5px solid #98A2B3; }
QFrame#taskRow[editorHidden="true"] {
    color: #7D8796;
    background: #20242C;
    border-style: dashed;
}
QFrame#taskGroupHeader {
    background: #203653;
    border: none;
    border-bottom: 1px solid #486C9B;
}
QFrame#taskGroupHeader[groupDepth="2"] { background: #263342; }
QFrame#taskGroupHeader[groupDepth="3"] { background: #292F38; }
QFrame#taskGroupHeader QLabel[role="groupTitle"] {
    color: #B8D4FF;
    font-weight: 700;
}
QFrame#taskRow[groupDepth="1"] { border-left: 3px solid #486C9B; }
QFrame#taskRow[groupDepth="2"] { border-left: 5px solid #5C82B4; }
QFrame#taskRow[groupDepth="3"] { border-left: 7px solid #78AFFF; }
QFrame#taskRow[groupStart="true"] { border-top-color: #5C82B4; }
QFrame#taskRow[groupEnd="true"] { border-bottom-color: #5C82B4; }
QFrame#taskRow[selected="true"] {
    background: #1E304A;
    border-top: 2px solid #5C9AFF;
    border-right: 2px solid #5C9AFF;
    border-bottom: 2px solid #5C9AFF;
}
QFrame#taskRow[selected="true"][markerColor=""] {
    border: 2px solid #5C9AFF;
}
QFrame#taskRow[dragSource="true"] {
    background: #17375F;
    border: 2px solid #78AFFF;
}
QLabel#taskDragSourceBadge {
    color: #DCE9FF;
    background: rgba(30, 48, 74, 244);
    border: 1px solid #5C9AFF;
}
QFrame#taskRow[debugPaused="true"] {
    background: #392C18;
    border-color: #E59A2F;
}
QFrame#taskRow QLabel#stepIndex {
    color: #9EA8B8;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #596476;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background: #79869A;
}
QSplitter::handle {
    background: #353C48;
}
QSplitter::handle:hover {
    background: #4A668B;
}
QGroupBox {
    border-color: #3B4351;
}
QGroupBox::title {
    color: #D6DCE6;
    background: #1B1F27;
}
QMenu {
    color: #E6EAF0;
    background: #1B1F27;
    border-color: #3B4351;
}
QMenu::item:selected {
    color: #FFFFFF;
    background: #34547E;
}
QToolTip {
    color: #FFFFFF;
    background: #263241;
    border-color: #59697D;
}
"""


_STYLE_LENGTH_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>px|pt)")

_REFERENCE_DPI_PROPERTY = "_fukua_reference_logical_dpi"
_WINDOW_DENSITY_PROPERTY = "_fukua_screen_visual_density"
_MIN_VISUAL_DENSITY = 0.5
_MAX_VISUAL_DENSITY = 3.0
_QWIDGET_MAX_SIZE = 16777215
_INLINE_STYLESHEET_BASE_SAVED = "_fukua_inline_stylesheet_base_saved"
_INLINE_STYLESHEET_BASE = "_fukua_inline_stylesheet_base"
_INLINE_STYLESHEET_LAST = "_fukua_inline_stylesheet_last_scaled"


def visual_density_ratio(target_dpi, reference_dpi) -> float:
    """Return a bounded visual-size ratio without touching screen coordinates."""

    try:
        target = float(target_dpi)
        reference = float(reference_dpi)
    except (TypeError, ValueError):
        return 1.0
    if (
        not math.isfinite(target)
        or not math.isfinite(reference)
        or target <= 0.0
        or reference <= 0.0
    ):
        return 1.0
    return max(
        _MIN_VISUAL_DENSITY,
        min(_MAX_VISUAL_DENSITY, target / reference),
    )


def _screen_logical_dpi(screen) -> float:
    if screen is None:
        return 96.0
    try:
        dpi = float(screen.logicalDotsPerInch())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 96.0
    return dpi if math.isfinite(dpi) and dpi > 0.0 else 96.0


def application_reference_dpi(application: QApplication | None = None) -> float:
    """Keep the primary screen's launch density as the visual design baseline."""

    candidate = application or QApplication.instance()
    if not isinstance(candidate, QApplication):
        return 96.0
    application = candidate
    stored = application.property(_REFERENCE_DPI_PROPERTY)
    try:
        stored_dpi = float(stored)
    except (TypeError, ValueError):
        stored_dpi = 0.0
    if math.isfinite(stored_dpi) and stored_dpi > 0.0:
        return stored_dpi
    reference = _screen_logical_dpi(application.primaryScreen())
    application.setProperty(_REFERENCE_DPI_PROPERTY, reference)
    return reference


def _eligible_top_level(widget: QWidget | None) -> bool:
    if widget is None or not isValid(widget):
        return False
    if not isinstance(widget, (QMainWindow, QDialog)):
        return False
    try:
        return not bool(widget.property("skipPerScreenFontRefresh"))
    except RuntimeError:
        return False


def window_visual_density(
    widget: QWidget | None,
    application: QApplication | None = None,
) -> float:
    """Return the visual metric ratio for an ordinary work window."""

    if widget is None:
        return 1.0
    try:
        top_level = widget if widget.isWindow() else widget.window()
    except RuntimeError:
        return 1.0
    if not _eligible_top_level(top_level):
        return 1.0
    screen = None
    try:
        handle = top_level.windowHandle()
        if handle is not None:
            screen = handle.screen()
        if screen is None:
            screen = top_level.screen()
    except RuntimeError:
        return 1.0
    candidate = application or QApplication.instance()
    application = candidate if isinstance(candidate, QApplication) else None
    return visual_density_ratio(
        _screen_logical_dpi(screen),
        application_reference_dpi(application),
    )


def apply_widget_geometry_scale(
    widgets,
    ui_scale,
    density_ratio: float | None = None,
) -> None:
    """Scale explicit widget constraints from immutable base dimensions."""

    user_scale = max(0.75, min(1.8, float(ui_scale)))
    for widget in widgets:
        if widget is None:
            continue
        try:
            density = (
                window_visual_density(widget)
                if density_ratio is None
                else float(density_ratio)
            )
            effective_scale = user_scale * max(
                _MIN_VISUAL_DENSITY,
                min(_MAX_VISUAL_DENSITY, density),
            )
            if not widget.property("_ui_scale_base_saved"):
                widget.setProperty("_ui_scale_base_saved", True)
                widget.setProperty("_ui_base_min_w", widget.minimumWidth())
                widget.setProperty("_ui_base_min_h", widget.minimumHeight())
                widget.setProperty("_ui_base_max_w", widget.maximumWidth())
                widget.setProperty("_ui_base_max_h", widget.maximumHeight())

            min_w = int(widget.property("_ui_base_min_w") or 0)
            min_h = int(widget.property("_ui_base_min_h") or 0)
            max_w = int(
                widget.property("_ui_base_max_w") or _QWIDGET_MAX_SIZE
            )
            max_h = int(
                widget.property("_ui_base_max_h") or _QWIDGET_MAX_SIZE
            )

            is_scalable_text_button = (
                isinstance(widget, QPushButton)
                and bool(widget.text().strip())
                and not bool(widget.property("iconOnly"))
                and widget.objectName() != "helpButton"
                and max_w >= _QWIDGET_MAX_SIZE
            )
            if is_scalable_text_button:
                base_text_width = widget.property("_ui_base_text_button_w")
                if base_text_width is None:
                    widget.ensurePolished()
                    base_text_width = max(
                        1,
                        int(round(widget.sizeHint().width() / effective_scale)),
                    )
                    widget.setProperty(
                        "_ui_base_text_button_w", base_text_width
                    )
                min_w = max(min_w, int(base_text_width))

            if min_w > 0:
                widget.setMinimumWidth(
                    max(1, int(round(min_w * effective_scale)))
                )
                if is_scalable_text_button:
                    content_width = widget.sizeHint().width()
                    if content_width > widget.minimumWidth():
                        widget.setMinimumWidth(content_width)
            if min_h > 0:
                widget.setMinimumHeight(
                    max(1, int(round(min_h * effective_scale)))
                )
            if 0 < max_w < _QWIDGET_MAX_SIZE:
                widget.setMaximumWidth(
                    max(1, int(round(max_w * effective_scale)))
                )
            if 0 < max_h < _QWIDGET_MAX_SIZE:
                widget.setMaximumHeight(
                    max(1, int(round(max_h * effective_scale)))
                )
        except RuntimeError:
            # A top-level refresh may race a deleteLater() callback. The next
            # live widget still receives its own metrics, so a stale child is safe to skip.
            continue
        except Exception as exc:
            logger.debug(
                "应用逐屏控件尺寸失败: %s", exc, exc_info=True
            )
            continue


def _scaled_metric(value: int, density: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * density)))


def scale_stylesheet_lengths(
    stylesheet: str,
    font_scale=1.0,
    *,
    metric_scale=None,
) -> str:
    """Scale QSS point fonts and pixel geometry from an immutable source."""

    font_scale = max(0.75, min(1.8, float(font_scale)))
    metric_scale = (
        font_scale
        if metric_scale is None
        else max(0.375, min(5.4, float(metric_scale)))
    )

    def replace_length(match):
        unit = match.group("unit")
        value = float(match.group("value")) * (
            metric_scale if unit == "px" else font_scale
        )
        if unit == "px":
            return f"{max(1, int(round(value)))}px"
        text = f"{max(1.0, value):.2f}".rstrip("0").rstrip(".")
        return f"{text}pt"

    return _STYLE_LENGTH_PATTERN.sub(replace_length, str(stylesheet or ""))


def _refresh_inline_stylesheet(widget, ui_scale: float, density: float) -> None:
    """Refresh a child widget's own QSS without accumulating scale factors."""

    current = widget.styleSheet()
    last_scaled = widget.property(_INLINE_STYLESHEET_LAST)
    if not bool(widget.property(_INLINE_STYLESHEET_BASE_SAVED)):
        widget.setProperty(_INLINE_STYLESHEET_BASE_SAVED, True)
        widget.setProperty(_INLINE_STYLESHEET_BASE, current)
    elif last_scaled is not None and current != str(last_scaled):
        # Runtime state may replace an inline stylesheet between screen changes.
        # Treat that replacement as a fresh, unscaled source style.
        widget.setProperty(_INLINE_STYLESHEET_BASE, current)
    base = str(widget.property(_INLINE_STYLESHEET_BASE) or "")
    target = scale_stylesheet_lengths(
        base,
        ui_scale,
        metric_scale=ui_scale * density,
    )
    if current != target:
        widget.setStyleSheet(target)
    widget.setProperty(_INLINE_STYLESHEET_LAST, target)


def _layout_metric_state(layout, key: str, current):
    base_key = f"_fukua_density_base_{key}"
    last_key = f"_fukua_density_last_{key}"
    base = layout.property(base_key)
    last = layout.property(last_key)
    normalized = tuple(current) if isinstance(current, (tuple, list)) else int(current)
    normalized_last = (
        tuple(last) if isinstance(last, (tuple, list)) else last
    )
    if base is None or (last is not None and normalized != normalized_last):
        base = normalized
        layout.setProperty(base_key, base)
    elif isinstance(base, (tuple, list)):
        base = tuple(base)
    else:
        base = int(base)
    return base, last_key


def _scale_layout(layout: QLayout, density: float) -> None:
    margins = layout.contentsMargins()
    current_margins = (
        margins.left(),
        margins.top(),
        margins.right(),
        margins.bottom(),
    )
    base_margins, last_margins_key = _layout_metric_state(
        layout, "margins", current_margins
    )
    if not isinstance(base_margins, tuple):
        base_margins = current_margins
    target_margins = tuple(
        _scaled_metric(int(value), density) for value in base_margins
    )
    if current_margins != target_margins:
        layout.setContentsMargins(*target_margins)
    layout.setProperty(last_margins_key, target_margins)

    horizontal_getter = getattr(layout, "horizontalSpacing", None)
    vertical_getter = getattr(layout, "verticalSpacing", None)
    horizontal_setter = getattr(layout, "setHorizontalSpacing", None)
    vertical_setter = getattr(layout, "setVerticalSpacing", None)
    has_axis_spacing = (
        callable(horizontal_getter)
        and callable(vertical_getter)
        and callable(horizontal_setter)
        and callable(vertical_setter)
    )
    if has_axis_spacing:
        def scale_axis(key, getter, setter):
            if not callable(getter) or not callable(setter):
                return
            current = int(getter())
            if current < 0:
                return
            base, last_key = _layout_metric_state(layout, key, current)
            target = _scaled_metric(int(base), density)
            if current != target:
                setter(target)
            layout.setProperty(last_key, target)

        scale_axis(
            "horizontal_spacing", horizontal_getter, horizontal_setter
        )
        scale_axis("vertical_spacing", vertical_getter, vertical_setter)
    else:
        current = int(layout.spacing())
        if current >= 0:
            base, last_key = _layout_metric_state(layout, "spacing", current)
            target = _scaled_metric(int(base), density)
            if current != target:
                layout.setSpacing(target)
            layout.setProperty(last_key, target)
    layout.invalidate()


def apply_layout_density_scale(root: QWidget, density_ratio: float) -> None:
    """Scale code-defined layout gaps once from their unscaled source values."""

    density = max(
        _MIN_VISUAL_DENSITY,
        min(_MAX_VISUAL_DENSITY, float(density_ratio)),
    )
    layouts = []
    layout_accessor = getattr(root, "layout", None)
    root_layout = (
        layout_accessor()
        if callable(layout_accessor)
        else layout_accessor
        if isinstance(layout_accessor, QLayout)
        else None
    )
    if root_layout is not None:
        layouts.append(root_layout)
    layouts.extend(root.findChildren(QLayout))
    seen = set()
    for layout in layouts:
        identity = id(layout)
        if identity in seen:
            continue
        seen.add(identity)
        _scale_layout(layout, density)
    root.updateGeometry()


class _PerScreenFontRefresh(QObject):
    """Refresh fonts and visual metrics after a mixed-DPI screen change.

    The application deliberately keeps Qt's high-DPI geometry scaling disabled:
    screenshot overlays and injected input share physical desktop pixels.  Qt
    still reports each ordinary work window's target DPI.  Fonts, stylesheet
    pixels, explicit widget constraints and layout gaps are therefore refreshed
    together relative to the primary-screen design baseline.  This changes only
    control density; screenshot, preview and input coordinate spaces remain
    untouched.
    """

    def __init__(self, application: QApplication):
        super().__init__(application)
        self._application_ref = weakref.ref(application)
        application.installEventFilter(self)
        application.screenAdded.connect(self._connect_screen)
        for screen in application.screens():
            self._connect_screen(screen)

    def _application(self) -> QApplication | None:
        return self._application_ref()

    @staticmethod
    def _eligible(widget: QWidget) -> bool:
        return _eligible_top_level(widget)

    def eventFilter(self, watched, event):
        if (
            isinstance(watched, QWidget)
            and watched.isWindow()
            and event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange)
            and self._eligible(watched)
        ):
            widget_ref = weakref.ref(watched)
            QTimer.singleShot(0, lambda ref=widget_ref: self._track(ref))
        return False

    def _track(self, widget_ref) -> None:
        widget = widget_ref()
        if (
            widget is None
            or not self._eligible(widget)
            or not widget.isVisible()
        ):
            return
        handle = widget.windowHandle()
        # WinIdChange may replace the QWindow while preserving the QWidget.
        # Store the marker on the concrete native handle so every replacement
        # receives its own screenChanged connection.
        if handle is not None and not bool(
            handle.property("_fukua_screen_signal_connected")
        ):
            handle.setProperty("_fukua_screen_signal_connected", True)
            handle.screenChanged.connect(
                lambda _screen, ref=widget_ref: self._queue_refresh(ref)
            )
        self.refresh_widget(widget)

    def _connect_screen(self, screen) -> None:
        if screen is None or bool(screen.property("_fukua_dpi_signal_connected")):
            return
        screen.setProperty("_fukua_dpi_signal_connected", True)
        screen.logicalDotsPerInchChanged.connect(
            lambda _dpi, target=screen: self.refresh_screen(target)
        )

    def _queue_refresh(self, widget_ref) -> None:
        # WM_DPICHANGED may still be updating the native window when
        # QWindow.screenChanged fires. Defer and coalesce one event-loop turn so
        # metrics use the destination screen without repolishing the tree twice.
        widget = widget_ref()
        if widget is None or not isValid(widget):
            return
        if bool(widget.property("_fukua_screen_refresh_pending")):
            return
        widget.setProperty("_fukua_screen_refresh_pending", True)
        QTimer.singleShot(0, lambda ref=widget_ref: self._refresh_ref(ref))

    def _refresh_ref(self, widget_ref) -> None:
        widget = widget_ref()
        if widget is not None and isValid(widget):
            widget.setProperty("_fukua_screen_refresh_pending", False)
            self.refresh_widget(widget)

    def refresh_screen(self, screen) -> None:
        application = self._application()
        if application is None:
            return
        for widget in application.topLevelWidgets():
            handle = widget.windowHandle() if isinstance(widget, QWidget) else None
            if (
                widget.isVisible()
                and handle is not None
                and handle.screen() is screen
            ):
                self.refresh_widget(widget)

    def refresh_all(self) -> None:
        application = self._application()
        if application is None:
            return
        for widget in application.topLevelWidgets():
            # Constructors can call apply_modern_theme() before fixed sizes and
            # layouts exist. Show/WinIdChange will establish the real baseline.
            if widget.isVisible():
                self.refresh_widget(widget)

    def refresh_widget(self, widget: QWidget) -> None:
        application = self._application()
        if application is None or not self._eligible(widget):
            return
        density = window_visual_density(widget, application)
        ui_scale = max(
            0.75,
            min(
                1.8,
                float(application.property("_fukua_ui_scale") or 1.0),
            ),
        )
        mode = normalize_theme_mode(
            application.property("_fukua_theme_mode") or "light"
        )
        try:
            children = (widget, *widget.findChildren(QWidget))
        except RuntimeError:
            return
        uncached_children = tuple(
            child
            for child in children
            if isValid(child)
            and not bool(child.property("_ui_scale_base_saved"))
        )
        if uncached_children:
            # Preserve code-defined constraints before a first-show window on
            # a non-reference screen receives its density stylesheet.  Doing
            # this afterwards would treat already-scaled QSS constraints as
            # source values and shrink fixed controls twice.
            apply_widget_geometry_scale(
                uncached_children,
                ui_scale,
                1.0,
            )
        current_stylesheet = widget.styleSheet()
        last_density_stylesheet = widget.property(
            "_fukua_last_density_stylesheet"
        )
        if not bool(widget.property("_fukua_base_local_stylesheet_saved")):
            widget.setProperty("_fukua_base_local_stylesheet_saved", True)
            widget.setProperty(
                "_fukua_base_local_stylesheet", current_stylesheet
            )
        elif (
            last_density_stylesheet is not None
            and current_stylesheet != str(last_density_stylesheet)
        ):
            # A window may install a purpose-specific local stylesheet after
            # first show. Treat that external replacement as the new base.
            widget.setProperty(
                "_fukua_base_local_stylesheet", current_stylesheet
            )
        base_local_stylesheet = str(
            widget.property("_fukua_base_local_stylesheet") or ""
        )
        scaled_base_local_stylesheet = scale_stylesheet_lengths(
            base_local_stylesheet,
            ui_scale,
            metric_scale=ui_scale * density,
        )
        if abs(density - 1.0) > 0.001:
            density_stylesheet = build_modern_stylesheet(
                ui_scale,
                mode,
                metric_scale=ui_scale * density,
            )
            target_stylesheet = (
                f"{density_stylesheet}\n{scaled_base_local_stylesheet}"
                if scaled_base_local_stylesheet
                else density_stylesheet
            )
        else:
            target_stylesheet = scaled_base_local_stylesheet
        if current_stylesheet != target_stylesheet:
            widget.setStyleSheet(target_stylesheet)
        widget.setProperty(
            "_fukua_last_density_stylesheet", target_stylesheet
        )
        widget.setProperty(_WINDOW_DENSITY_PROPERTY, density)

        for child in children[1:]:
            if isValid(child):
                _refresh_inline_stylesheet(child, ui_scale, density)

        # A QFont copy carries Qt's already-resolved engine/cache from the old
        # screen.  Serializing and rebuilding preserves the semantic font
        # attributes (family, size, weight, style) while forcing resolution
        # against the destination window's DPI.  Re-polishing also reapplies
        # any QSS font-size rule without replacing bespoke local stylesheets.
        for child in children:
            if not isValid(child):
                continue
            old_font = child.font()
            resolve_mask = old_font.resolveMask()
            fresh_font = QFont()
            if not fresh_font.fromString(old_font.toString()):
                fresh_font = QFont(application.font())
            # Preserve which attributes were explicit.  Without restoring the
            # resolve mask, assigning the rebuilt font would freeze inherited
            # point sizes and a later user interface-scale change could no
            # longer update ordinary labels and controls.
            fresh_font.setResolveMask(resolve_mask)
            child.setFont(fresh_font)
            style = child.style()
            style.unpolish(child)
            style.polish(child)
            child.updateGeometry()
        apply_layout_density_scale(widget, density)
        apply_widget_geometry_scale(children, ui_scale, density)
        density_callback = getattr(
            widget, "on_per_screen_visual_density_changed", None
        )
        if callable(density_callback):
            density_callback(density)
        widget.updateGeometry()


def install_per_screen_font_refresh(application: QApplication) -> _PerScreenFontRefresh:
    manager = application.property("_fukua_screen_font_manager")
    if not isinstance(manager, _PerScreenFontRefresh):
        manager = _PerScreenFontRefresh(application)
        application.setProperty("_fukua_screen_font_manager", manager)
    return manager


def normalize_theme_mode(mode):
    return "dark" if str(mode).strip().lower() == "dark" else "light"


def build_modern_stylesheet(
    scale=1.0,
    mode="light",
    *,
    metric_scale=None,
):
    """Scale font and geometry rules independently from the immutable theme."""

    stylesheet = APP_STYLESHEET
    if normalize_theme_mode(mode) == "dark":
        stylesheet += DARK_STYLESHEET
    return scale_stylesheet_lengths(
        stylesheet,
        scale,
        metric_scale=metric_scale,
    )


def apply_modern_theme(application, scale=1.0, mode="light"):
    scale = max(0.75, min(1.8, float(scale)))
    mode = normalize_theme_mode(mode)
    application.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPointSizeF(9.0 * scale)
    application.setFont(font)

    palette = QPalette()
    if mode == "dark":
        palette.setColor(QPalette.Window, QColor("#111318"))
        palette.setColor(QPalette.WindowText, QColor("#E6EAF0"))
        palette.setColor(QPalette.Base, QColor("#1B1F27"))
        palette.setColor(QPalette.AlternateBase, QColor("#161A22"))
        palette.setColor(QPalette.Text, QColor("#E6EAF0"))
        palette.setColor(QPalette.Button, QColor("#1B1F27"))
        palette.setColor(QPalette.ButtonText, QColor("#D6DCE6"))
        palette.setColor(QPalette.Highlight, QColor("#34547E"))
        palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#727C8C"))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#727C8C"))
    else:
        palette.setColor(QPalette.Window, QColor("#F4F6F8"))
        palette.setColor(QPalette.WindowText, QColor("#172033"))
        palette.setColor(QPalette.Base, QColor("#FFFFFF"))
        palette.setColor(QPalette.AlternateBase, QColor("#F8FAFC"))
        palette.setColor(QPalette.Text, QColor("#172033"))
        palette.setColor(QPalette.Button, QColor("#FFFFFF"))
        palette.setColor(QPalette.ButtonText, QColor("#344054"))
        palette.setColor(QPalette.Highlight, QColor("#DCEAFF"))
        palette.setColor(QPalette.HighlightedText, QColor("#172033"))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#98A2B3"))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#98A2B3"))
    application.setPalette(palette)
    application.setStyleSheet(build_modern_stylesheet(scale, mode))
    application.setProperty("_fukua_modern_theme", True)
    application.setProperty("_fukua_ui_scale", scale)
    application.setProperty("_fukua_theme_mode", mode)
    application_reference_dpi(application)
    manager = install_per_screen_font_refresh(application)
    manager.refresh_all()
