#!/usr/bin/env python3
"""D15-001 回归：5 项全局设置控件（点击偏移 / 移动曲线 / 按键按住 / 限时停止 /
独立 Runner）应接入脏标记信号，修改后触发 ``log_setting_change``，使 3s 自动保存落盘。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit

from fukua_rpa.ui.controllers.log_controller import LogController

_APP = QApplication.instance() or QApplication([])


class _GenericWidget(QLineEdit):
    """bind_setting_logs 访问到的未知 host 控件的通用占位：补齐可能连接的信号与
    方法。同一实例兼作“控件”与“可调用方法”的占位——bind_setting_logs 既会把
    host.<widget>.<signal> 连接到 lambda，也会把 host.<method> 直接作为 connect
    目标传入；使占位对象可调用即可同时满足两种用法。
    """

    stateChanged = Signal(int)
    currentTextChanged = Signal(str)
    currentIndexChanged = Signal(int)
    toggled = Signal(bool)
    valueChanged = Signal(int)
    clicked = Signal(bool)

    def __call__(self, *args, **kwargs):
        # 作为 host.<method> 的 connect 目标时，连接本身不触发调用，no-op 即可。
        return None

    def setCurrentIndex(self, i):
        pass

    def currentIndex(self):
        return 0

    def currentData(self):
        return None

    def addItem(self, *a, **k):
        pass

    def findData(self, *a, **k):
        return -1

    def setEnabled(self, *a, **k):
        pass

    def setVisible(self, *a, **k):
        pass

    def setToolTip(self, *a, **k):
        pass


class _FakeHost:
    def __init__(self):
        # 5 个目标控件用真实类型，便于正确 emit 信号并断言。
        self.click_spatial_jitter = QLineEdit("0")
        self.move_curve_strength = QLineEdit("0")
        self.key_hold_duration = QLineEdit("0")
        self.time_limit_stop_mode_combo = QComboBox()
        self.time_limit_stop_mode_combo.addItem("A", "a")
        self.time_limit_stop_mode_combo.addItem("B", "b")
        self.isolated_runner_chk = QCheckBox()
        self._calls = []
        self._generic = _GenericWidget()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # 任何未在 __init__ 显式创建的 host 属性，返回通用占位控件。
        return self._generic

    def log_setting_change(self, name, value, *, sensitive=None):
        self._calls.append((name, value))

    def apply_ui_scale_from_edit(self):
        pass

    def update_native_optimization_ui(self, *a):
        pass

    def refresh_scale_memory_status(self, *a):
        pass


class SettingsDirtyMarkTests(unittest.TestCase):
    def test_five_controls_trigger_dirty_mark(self):
        host = _FakeHost()
        ctrl = LogController(host)
        ctrl.bind_setting_logs()

        host.click_spatial_jitter.setText("3")
        host.click_spatial_jitter.editingFinished.emit()
        host.move_curve_strength.setText("0.5")
        host.move_curve_strength.editingFinished.emit()
        host.key_hold_duration.setText("1")
        host.key_hold_duration.editingFinished.emit()
        host.time_limit_stop_mode_combo.setCurrentIndex(1)
        host.isolated_runner_chk.setChecked(True)

        names = {call[0] for call in host._calls}
        self.assertIn("点击偏移", names)
        self.assertIn("移动曲线", names)
        self.assertIn("按键按住", names)
        self.assertIn("限时停止", names)
        self.assertIn("独立Runner", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
