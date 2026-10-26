# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""RegionWindow 生命周期集中管理。

机械收口：所有打开区域选择窗口的入口统一走本模块，窗口的创建、
选中信号连接、销毁后引用清理均在此处理；各控制器/窗口不再直接
``RegionWindow(...)`` 实例化。不改变任何业务行为。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, ref

from .lifecycle import safe_close

if TYPE_CHECKING:

    from .input_tools import RegionWindow


class RegionWindowManager:
    """按 (host, key) 跟踪并管理活跃 ``RegionWindow`` 的生命周期。"""

    def __init__(self) -> None:
        # host -> {key: weakref(RegionWindow)}
        self._tracked: WeakKeyDictionary = WeakKeyDictionary()

    def open(
        self,
        *,
        host: Any,
        key: Any,
        multi: bool = False,
        purpose: str = "recognition",
        restrict_rect: tuple | None = None,
        on_selected: Callable | None = None,
        multi_select: bool = False,
        on_closed: Callable | None = None,
    ) -> RegionWindow:
        """创建并跟踪一个 ``RegionWindow``，连接选中信号与销毁清理。"""
        from .input_tools import RegionWindow

        win = RegionWindow(multi=multi, purpose=purpose, restrict_rect=restrict_rect)
        bucket = self._tracked.setdefault(host, {})
        bucket[key] = ref(win)
        if on_selected is not None:
            signal = win.regions_selected if multi_select else win.region_selected
            signal.connect(on_selected)

        def _cleanup(*_args: object) -> None:
            bucket_local = self._tracked.get(host)
            if bucket_local is not None:
                bucket_local.pop(key, None)
            if on_closed is not None:
                on_closed()

        win.destroyed.connect(_cleanup)
        return win

    def get(self, host: Any, key: Any) -> RegionWindow | None:
        """返回当前跟踪的窗口（若已销毁则返回 ``None``）。"""
        bucket = self._tracked.get(host)
        if not bucket:
            return None
        weak = bucket.get(key)
        return weak() if weak is not None else None

    def is_open(self, host: Any, key: Any) -> bool:
        return self.get(host, key) is not None

    def close(self, host: Any, key: Any) -> None:
        """安全关闭指定 (host, key) 的窗口（若打开）。"""
        win = self.get(host, key)
        if win is not None:
            safe_close(win)

    def close_for_host(self, host: Any) -> None:
        """关闭某 host 下的全部区域窗口。"""
        bucket = self._tracked.get(host)
        if not bucket:
            return
        for win in [r() for r in list(bucket.values())]:
            if win is not None:
                safe_close(win)
        bucket.clear()

    def close_all(self) -> None:
        """关闭所有跟踪中的区域窗口。"""
        for host in list(self._tracked.keys()):
            self.close_for_host(host)


region_window_manager = RegionWindowManager()
