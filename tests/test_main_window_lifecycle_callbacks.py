"""FR-20260928-003 回归测试：主窗口两处裸 QTimer.singleShot 未随宿主销毁取消。

修复前：``RPAWindow._init_window_geometry``（上次会话未正常退出提示，800ms）与
``RPAWindow._on_scheduled_missed_ask``（错过补跑询问，0ms）使用裸
``QTimer.singleShot``，宿主销毁后回调仍会触达已失效控件，违反
``docs/UI开发规范.md`` 生命周期第 3 条（延迟回调随宿主销毁取消）。

探针方式：以 ``RPAWindow.__new__`` 构造最小宿主（不进入完整工作区初始化），
仅执行被测方法；宿主销毁后冲刷事件循环，断言回调不再触达
``QMessageBox``/``resolve_missed_ask``；宿主存活时断言提示/询问路径正常。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from fukua_rpa.ui import main_window as mw_module
from fukua_rpa.ui.main_window import RPAWindow


def _destroy_and_flush(app: QApplication, widget) -> None:
    """真正销毁 widget（processEvents 不处理 DeferredDelete，需显式派发）。"""
    widget.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _make_stub() -> RPAWindow:
    """构造最小 RPAWindow 宿主：仅初始化 QMainWindow 基类，不进入完整工作区。"""
    stub = RPAWindow.__new__(RPAWindow)
    QMainWindow.__init__(stub)
    return stub


def _flush_ms(app: QApplication, ms: int) -> None:
    """持续冲刷事件循环约 ms 毫秒，覆盖 800ms 延迟定时器窗口。"""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


class _SessionStub:
    """ApplicationSession 替身：previous_unclean 可控，避免真实锁文件语义。"""

    def __init__(self, unclean: bool):
        self.previous_unclean = unclean

    def start(self):
        return self


class TestUncleanExitPromptLifecycle(unittest.TestCase):
    """上次会话未正常退出提示（main_window.py _init_window_geometry 内 800ms 提示）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_geometry(self, stub: RPAWindow) -> None:
        fake_session_cls = MagicMock(return_value=_SessionStub(unclean=True))
        with patch.object(mw_module, "ApplicationSession", fake_session_cls):
            stub._init_window_geometry(self._tmp)

    def test_prompt_skipped_after_host_destroyed(self):
        stub = _make_stub()
        self._run_geometry(stub)
        _destroy_and_flush(self.app, stub)  # 销毁宿主 C++ 对象
        with patch.object(QMessageBox, "information", MagicMock()) as info:
            _flush_ms(self.app, 1000)
            self.assertEqual(
                info.call_count,
                0,
                "宿主销毁后 800ms 提示回调不应再触达 QMessageBox（FR-20260928-003）",
            )

    def test_prompt_shown_when_host_alive(self):
        stub = _make_stub()
        self._run_geometry(stub)
        with patch.object(QMessageBox, "information", MagicMock()) as info:
            _flush_ms(self.app, 1000)
            self.assertEqual(info.call_count, 1)
            self.assertEqual(info.call_args.args[1], "上次会话未正常退出")
        # 清理：最小宿主没有 shutdown_controller，不能走完整 closeEvent。
        _destroy_and_flush(self.app, stub)


class TestMissedAskPromptLifecycle(unittest.TestCase):
    """错过补跑询问（main_window.py _on_scheduled_missed_ask 内 0ms 询问）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_ask_skipped_after_host_destroyed(self):
        stub = _make_stub()
        service = MagicMock()
        stub.scheduled_service = service
        # 实例级 isVisible 探针：C++ 对象销毁后仍可安全调用，使红/绿判定
        # 确定（不依赖 PySide 对已删对象方法调用的异常传播方式）。
        stub.isVisible = lambda: False
        stub._on_scheduled_missed_ask("s-missed-1", "任务甲")
        # 0ms 定时器会在任何普通 processEvents 中先行触发（此时宿主仍存活），
        # 因此销毁时只派发 DeferredDelete，不先跑常规事件循环——模拟
        # "定时器尚未到点宿主即被销毁" 的真实缺陷场景。
        stub.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        with patch.object(QMessageBox, "question", MagicMock()):
            _flush_ms(self.app, 200)
            self.assertEqual(
                service.resolve_missed_ask.call_count,
                0,
                "宿主销毁后错过补跑询问不应再触达 scheduled_service（FR-20260928-003）",
            )

    def test_ask_runs_once_when_host_alive(self):
        stub = _make_stub()
        service = MagicMock()
        stub.scheduled_service = service
        stub.isVisible = lambda: False  # 未显示 → 按跳过处理并记账
        stub._on_scheduled_missed_ask("s-missed-2", "任务乙")
        _flush_ms(self.app, 200)
        service.resolve_missed_ask.assert_called_once_with("s-missed-2", False)

    def test_ask_callback_uses_owner_argument(self):
        """FR-20260930-004：`_ask` 必须经 owner 参数访问宿主，不得自捕获 self。

        用**与宿主不同的 owner** 触发已排定的回调：修复后记账落在传入的 owner 上，
        修复前 `_ask()` 忽略 owner、始终作用于原宿主，故本用例在修复前失败。
        """
        stub = _make_stub()
        stub.scheduled_service = MagicMock()
        stub.isVisible = lambda: True  # 原始宿主可见
        captured = {}

        def spy(widget, delay, callback):
            # 不真正排定定时器：本用例只验证回调的 owner 语义，避免残留 0ms
            # 定时器在宿主销毁时触发。
            captured["callback"] = callback

        with patch.object(mw_module, "schedule_widget_callback", spy):
            stub._on_scheduled_missed_ask("s-owner", "任务丁")
        callback = captured["callback"]

        other = _make_stub()
        other.scheduled_service = MagicMock()
        other.isVisible = lambda: False  # 传入 owner 不可见 → 按跳过记账
        with patch.object(QMessageBox, "question", MagicMock()):
            callback(other)
        other.scheduled_service.resolve_missed_ask.assert_called_once_with(
            "s-owner", False
        )
        stub.scheduled_service.resolve_missed_ask.assert_not_called()
        _destroy_and_flush(self.app, stub)
        _destroy_and_flush(self.app, other)


if __name__ == "__main__":
    unittest.main()
