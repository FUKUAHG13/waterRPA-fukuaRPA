"""P5b-2 守卫测试：RegionWindowManager 收口区域窗口生命周期。

用 stub 替换真实 RegionWindow（避免依赖 GUI/屏幕），验证：
- 模块层不引入 PySide6 / input_tools（仅 typing + weakref + lifecycle 间接）
- open 创建并跟踪窗口、连接选中信号（单/多）
- 窗口销毁后自动清理跟踪（destroyed -> 内部字典移除）
- close_for_host / close_all 批量关闭
- 业务代码不再直接实例化 RegionWindow（仅 input_tools 定义 + manager 内部）
"""

from __future__ import annotations

import inspect
import os
import subprocess

import pytest

from fukua_rpa.ui.region_window_manager import (
    RegionWindowManager,
    region_window_manager,
)


class _StubSignal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, fn) -> None:
        self._slots.append(fn)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class _StubRegionWindow:
    def __init__(self, multi=False, purpose="recognition", restrict_rect=None,
                 frozen_snapshot=None) -> None:
        self.multi = multi
        self.purpose = purpose
        self.restrict_rect = restrict_rect
        self.region_selected = _StubSignal()
        self.regions_selected = _StubSignal()
        self.destroyed = _StubSignal()
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.destroyed.emit()


class _StubHost:
    """可被弱引用（object() 实例不可弱引用，WeakKeyDictionary 要求 host 可弱引用）。

    真实宿主（RPAWindow / 对话框 / 行对象）均为 Qt QObject，天然可弱引用。
    """


@pytest.fixture
def patched(monkeypatch):
    import fukua_rpa.ui.input_tools as it

    monkeypatch.setattr(it, "RegionWindow", _StubRegionWindow)
    yield
    region_window_manager._tracked.clear()


def test_module_imports_only_typing_and_lifecycle():
    import fukua_rpa.ui.region_window_manager as m

    src = inspect.getsource(m)
    top = src.split("if TYPE_CHECKING:")[0]
    assert "import PySide6" not in top
    assert "from .input_tools import" not in top


def test_singleton_instance_exported():
    assert isinstance(region_window_manager, RegionWindowManager)


def test_open_tracks_window(patched):
    host = _StubHost()
    win = region_window_manager.open(host=host, key="k", purpose="p")
    assert isinstance(win, _StubRegionWindow)
    assert region_window_manager.is_open(host, "k") is True
    assert region_window_manager.get(host, "k") is win


def test_selected_signal_connected(patched):
    host = _StubHost()
    received: list = []
    win = region_window_manager.open(
        host=host, key="k", purpose="p",
        on_selected=lambda r: received.append(r),
    )
    win.region_selected.emit("RECT")
    assert received == ["RECT"]


def test_multi_select_uses_regions_signal(patched):
    host = _StubHost()
    received: list = []
    win = region_window_manager.open(
        host=host, key="k", purpose="p", multi=True, multi_select=True,
        on_selected=lambda rs: received.append(rs),
    )
    win.regions_selected.emit(["A", "B"])
    assert received == [["A", "B"]]


def test_destroy_cleans_tracking(patched):
    host = _StubHost()
    win = region_window_manager.open(host=host, key="k")
    assert region_window_manager.is_open(host, "k") is True
    win.close()  # -> destroyed -> manager 清理内部字典
    assert region_window_manager.is_open(host, "k") is False
    assert region_window_manager.get(host, "k") is None


def test_close_for_host(patched):
    host = _StubHost()
    region_window_manager.open(host=host, key="a")
    region_window_manager.open(host=host, key="b")
    region_window_manager.close_for_host(host)
    assert region_window_manager.is_open(host, "a") is False
    assert region_window_manager.is_open(host, "b") is False


def test_keys_are_isolated(patched):
    host = _StubHost()
    wxa = region_window_manager.open(host=host, key="x")
    # Keep a strong reference: the manager stores windows as weakrefs, so
    # dropping the return value here let the "y" window be garbage-collected
    # and made is_open("y") report False. The isolation logic is correct;
    # this test just must not lose the reference.
    wy = region_window_manager.open(host=host, key="y")
    wxa.close()
    assert region_window_manager.is_open(host, "x") is False
    assert region_window_manager.is_open(host, "y") is True
    # wy must remain referenced so the weakref-tracked window is not collected
    # before the assertions above are evaluated.
    assert wy is not None
    assert region_window_manager.is_open(host, "x") is False
    assert region_window_manager.is_open(host, "y") is True


def test_no_direct_region_window_instantiation_in_business_code():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Git 返回的是仓库 UTF-8 文本，不能交给 Windows 当前 ANSI 代码页猜测。
    out = subprocess.check_output(
        ["git", "-C", root, "grep", "-n", "RegionWindow(", "--", "fukua_rpa/ui"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    allowed = {
        "fukua_rpa/ui/input_tools.py",
        "fukua_rpa/ui/region_window_manager.py",
    }
    for line in out.splitlines():
        fpath = line.split(":", 1)[0]
        assert fpath in allowed, f"业务代码直接实例化 RegionWindow: {line}"
