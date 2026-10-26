"""Win32 window binding and foreground/background click implementation."""

from __future__ import annotations

import ctypes
import logging
import os
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from typing import Any, cast

logger = logging.getLogger(__name__)

# FR-20261006-017：后台点击目标解析时的子窗口枚举上限。取值高于
# window_diagnostics 的 128、接近 uia_backend 的 384，以免误截断常见控件树；
# 仅在病态目标上触发截断，截断后按已收集候选继续评分，语义不变。
_CHILD_ENUM_MAX_CANDIDATES = 512

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from .pyautogui_runtime import pyautogui as _pyautogui_proxy  # noqa: E402
from .timing_constants import MAPPING_POLL_INTERVAL_S, POLL_INTERVAL_SLOW_S  # noqa: E402
from .win32_api import (  # noqa: E402
    GA_ROOT,
    MK_LBUTTON,
    MK_RBUTTON,
    POINT,
    RECT,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_RBUTTONDBLCLK,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WNDENUMPROC,
    get_window_title,
    make_mouse_lparam,
    user32,
)
from .window_diagnostics import WindowInspector  # noqa: E402

# The lazy proxy's __getattr__ returns object; cast to Any so pyright
# accepts method calls without per-line suppression.
pyautogui = cast(Any, _pyautogui_proxy)


# Delays (seconds) inserted between posted mouse messages so the target window
# processes them in order. The longer pause after a button-up gives the target
# time to finish handling the click before the next message arrives.
_POST_MESSAGE_INTERVAL_S = 0.01
_POST_MESSAGE_INTERVAL_AFTER_UP_S = 0.02


class WindowBindingError(RuntimeError):
    """User-facing failure raised when a background-click binding cannot be made."""


class WindowMappingBackend:
    """Bind explicit Win32 target windows and replay clicks without activation.

    The backend resolves a saved binding to a concrete child HWND plus
    client-area coordinates, then delivers clicks through UI Automation
    first and ``PostMessage`` as the deterministic fallback. It never
    moves the real mouse for background clicks.
    """

    def __init__(self, process_id: int | None = None, uia_backend: Any | None = None) -> None:
        """Initialize the backend for one owning process.

        Args:
            process_id: PID treated as "self"; defaults to the current
                process so the backend never binds fukuaRPA's own windows.
            uia_backend: Optional UI Automation backend instance or test
                double; a real ``UIAutomationBackend`` is created when
                omitted.
        """
        self.process_id = int(process_id or os.getpid())
        self.inspector = WindowInspector(self.process_id)
        if uia_backend is None:
            from .uia_backend import UIAutomationBackend

            uia_backend = UIAutomationBackend()
        self.ui_automation = uia_backend
        # last_background_method/detail: written by engine/mapping thread,
        # read by UI controllers. GIL guarantees atomic attribute read/write;
        # worst case is reading a stale diagnostic string (no data corruption).
        self.last_background_method = ""
        self.last_background_detail: dict[str, Any] = {}

    def close(self) -> None:
        """Release the underlying UI Automation worker thread."""
        self.ui_automation.close()

    def class_name(self, hwnd: int) -> str:
        """Return the Win32 class name of *hwnd*, or "" on failure."""
        if not hwnd:
            return ""
        buffer = ctypes.create_unicode_buffer(256)
        try:
            length = user32.GetClassNameW(hwnd, buffer, len(buffer))
            return buffer.value[:length] if length > 0 else ""
        except Exception as exc:
            logger.warning("获取窗口类名失败: %s", exc, exc_info=True)
            return ""

    def title(self, hwnd: int) -> str:
        """Return the window title of *hwnd*, or "" on failure."""
        if not hwnd:
            return ""
        try:
            return get_window_title(hwnd)
        except Exception as exc:
            logger.warning("获取窗口标题失败: %s", exc, exc_info=True)
            return ""

    def process_info(self, hwnd: int) -> tuple[int, str]:
        """Return ``(pid, process_path)`` for the process owning *hwnd*.

        The path is normalized via ``os.path.normcase`` for case-insensitive
        comparison; it is "" when psutil is unavailable or the lookup fails.
        """
        pid = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        except Exception as exc:
            logger.warning("获取窗口进程 ID 失败: %s", exc, exc_info=True)
            return 0, ""
        process_path = ""
        if HAS_PSUTIL and pid.value:
            try:
                process_path = os.path.normcase(os.path.abspath(psutil.Process(pid.value).exe()))
            except Exception as exc:
                logger.warning("读取进程路径失败 (PID %s): %s", pid.value, exc, exc_info=True)
                process_path = ""
        return int(pid.value), process_path

    def belongs_to_current_process(self, hwnd: int) -> bool:
        """Return True when *hwnd* is owned by this backend's process."""
        if not hwnd:
            return False
        pid, _path = self.process_info(hwnd)
        return bool(pid and pid == self.process_id)

    @staticmethod
    def screen_area_at_point(hwnd: int, x: int, y: int) -> int | None:
        """Return the visible pixel area of *hwnd* when it contains ``(x, y)``.

        Returns:
            Window width times height in pixels, or ``None`` when the window
            is invisible, has an empty rectangle, or does not contain the
            point.
        """
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return None
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        if not (rect.left <= int(x) < rect.right and rect.top <= int(y) < rect.bottom):
            return None
        return (rect.right - rect.left) * (rect.bottom - rect.top)

    def top_level_window_at_point(
        self, x: int, y: int, exclude_current_process: bool = False
    ) -> int | None:
        """Return the first top-level window covering ``(x, y)``.

        Args:
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            exclude_current_process: Skip windows owned by this process.

        Returns:
            The matching HWND, or ``None`` when enumeration finds nothing.
        """
        candidates: list[int] = []

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if exclude_current_process and self.belongs_to_current_process(hwnd):
                return True
            if self.screen_area_at_point(hwnd, x, y) is not None:
                candidates.append(int(hwnd))
            return True

        try:
            callback = WNDENUMPROC(enum_proc)
            user32.EnumWindows(callback, 0)
        except Exception as exc:
            logger.warning("枚举窗口失败: %s", exc, exc_info=True)
            return None
        return candidates[0] if candidates else None

    def root_window_at_point(
        self, x: int, y: int, exclude_current_process: bool = False
    ) -> int | None:
        """Return the root (GA_ROOT) window at ``(x, y)``.

        Falls back to a top-level enumeration when the root belongs to the
        current process and exclusion was requested.

        Returns:
            The root HWND, or ``None`` when no usable window exists there.
        """
        point = POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(point)
        root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        root_hwnd = root_hwnd or hwnd
        if root_hwnd and exclude_current_process and self.belongs_to_current_process(root_hwnd):
            root_hwnd = self.top_level_window_at_point(
                x, y, exclude_current_process=True
            )
        if not root_hwnd or not user32.IsWindow(root_hwnd):
            return None
        if exclude_current_process and self.belongs_to_current_process(root_hwnd):
            return None
        return int(root_hwnd)

    def background_click_target(
        self,
        x: int,
        y: int,
        expected_root: int | None = None,
        exclude_current_process: bool = False,
        *,
        belongs_callback: Callable[[int], bool] | None = None,
        top_level_callback: Callable[..., int | None] | None = None,
        area_callback: Callable[[int, int, int], int | None] | None = None,
    ) -> int | None:
        """Pick the deepest child window that should receive a background click.

        Candidate windows are scored by visible area so the smallest control
        covering ``(x, y)`` wins; ties fall back to enumeration order.

        Args:
            x: Horizontal screen coordinate of the click.
            y: Vertical screen coordinate of the click.
            expected_root: When given, restrict the search to this root HWND
                instead of resolving one from the screen point.
            exclude_current_process: Skip windows owned by this process.
            belongs_callback: Override for ``belongs_to_current_process``
                (test seam).
            top_level_callback: Override for ``top_level_window_at_point``
                (test seam).
            area_callback: Override for ``screen_area_at_point`` (test seam).

        Returns:
            The chosen child HWND, or ``None`` when no usable window exists.
        """
        belongs = belongs_callback or self.belongs_to_current_process
        find_top_level = top_level_callback or self.top_level_window_at_point
        area_at_point = area_callback or self.screen_area_at_point
        if expected_root:
            root_hwnd = int(expected_root)
            if not user32.IsWindow(root_hwnd):
                return None
            base_hwnd = root_hwnd
        else:
            point = POINT(int(x), int(y))
            base_hwnd = user32.WindowFromPoint(point)
            root_hwnd = user32.GetAncestor(base_hwnd, GA_ROOT) if base_hwnd else None
            root_hwnd = root_hwnd or base_hwnd
            if root_hwnd and exclude_current_process and belongs(root_hwnd):
                root_hwnd = find_top_level(x, y, exclude_current_process=True)
                base_hwnd = root_hwnd
            if not root_hwnd:
                return None
        candidates: list[tuple[int, int, int]] = []

        def add_candidate(hwnd: Any) -> None:
            if not hwnd:
                return
            try:
                if exclude_current_process and belongs(hwnd):
                    return
                area = area_at_point(hwnd, x, y)
                if area is not None:
                    candidates.append((area, len(candidates), hwnd))
            except Exception as exc:
                logger.warning("评估候选窗口失败: %s", exc, exc_info=True)

        add_candidate(root_hwnd)
        add_candidate(base_hwnd)

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            # FR-20261006-017：子窗口枚举此前无数量上限、无提前终止、无预算，
            # 每个子 HWND 都要做 GetWindowRect/IsWindowVisible。病态目标
            # （窗口化浏览器、多面板 IDE、虚拟化列表宿主）可达数十万子窗口，
            # 该工作发生在运行循环内会累积延迟。
            # 与同领域有界约定对齐：window_diagnostics 用 128 上限、
            # uia_backend 用 384 节点 + 1.5s 预算（ARCHITECTURE.md 与
            # AGENTS.md「外部线程、钩子和原生任务必须具有有界工作量」）。
            # 上限取 512 以免误截断常见控件树；截断时按**已收集候选**继续评分，
            # 保持既有「最小可见面积优先」语义不变。
            if len(candidates) >= _CHILD_ENUM_MAX_CANDIDATES:
                return False
            add_candidate(hwnd)
            return True

        try:
            callback = WNDENUMPROC(enum_proc)
            user32.EnumChildWindows(root_hwnd, callback, 0)
        except Exception as exc:
            logger.warning("枚举子窗口失败: %s", exc, exc_info=True)
        if not candidates:
            return base_hwnd
        candidates.sort(key=lambda item: (item[0], -item[1]))
        return candidates[0][2]

    def create_binding_for_root(self, root_hwnd: int, x: int, y: int) -> dict[str, Any]:
        """Build a persisted binding payload for an already-chosen root window.

        Args:
            root_hwnd: Top-level window the user selected.
            x: Horizontal screen coordinate of the click point.
            y: Vertical screen coordinate of the click point.

        Returns:
            A serializable dict capturing root/target HWNDs, class names,
            process identity and client-area coordinates.

        Raises:
            WindowBindingError: With a user-facing message when the window is
                invalid, belongs to fukuaRPA, or the point is outside the
                clickable client area.
        """
        root_hwnd = user32.GetAncestor(root_hwnd, GA_ROOT) or root_hwnd
        if not root_hwnd or not user32.IsWindow(root_hwnd):
            raise WindowBindingError("选中的目标窗口已经失效，请重新选择。")
        if self.belongs_to_current_process(root_hwnd):
            raise WindowBindingError("不能绑定 fukuaRPA 自身窗口，请选择其他程序。")
        if self.screen_area_at_point(root_hwnd, x, y) is None:
            raise WindowBindingError(
                "映射的点击坐标不在所选目标窗口范围内。请先用“取点”选择窗口中的点击位置，"
                "再用“选择窗口”点一下同一个目标程序。"
            )
        target_hwnd = self.background_click_target(x, y, expected_root=root_hwnd)
        if not target_hwnd:
            raise WindowBindingError(
                "无法在所选程序内找到点击坐标对应的窗口或控件。请确保目标程序没有最小化。"
            )
        root_point = POINT(int(x), int(y))
        target_point = POINT(int(x), int(y))
        if not user32.ScreenToClient(root_hwnd, ctypes.byref(root_point)) or not user32.ScreenToClient(
            target_hwnd, ctypes.byref(target_point)
        ):
            raise WindowBindingError("无法换算目标窗口的客户区坐标。")
        target_rect = RECT()
        if not user32.GetClientRect(target_hwnd, ctypes.byref(target_rect)) or not (
            target_rect.left <= target_point.x < target_rect.right
            and target_rect.top <= target_point.y < target_rect.bottom
        ):
            raise WindowBindingError(
                "点击坐标不在目标窗口的可点击客户区内，请重新取点。"
            )
        pid, process_path = self.process_info(root_hwnd)
        try:
            control_id = int(user32.GetDlgCtrlID(target_hwnd))
        except Exception as exc:
            logger.warning("获取控件 ID 失败: %s", exc, exc_info=True)
            control_id = 0
        return {
            "root_hwnd": int(root_hwnd),
            "target_hwnd": int(target_hwnd),
            "pid": pid,
            "process_path": process_path,
            "root_class": self.class_name(root_hwnd),
            "root_title": self.title(root_hwnd),
            "target_class": self.class_name(target_hwnd),
            "target_control_id": control_id if control_id > 0 else 0,
            "root_client_x": int(root_point.x),
            "root_client_y": int(root_point.y),
            "target_client_x": int(target_point.x),
            "target_client_y": int(target_point.y),
        }

    def create_binding_for_window_at_point(
        self, selection_x: int, selection_y: int, click_x: int, click_y: int
    ) -> dict[str, Any]:
        """Create a binding from a window-selection click and a separate point.

        Args:
            selection_x: Screen X where the user clicked to choose a window.
            selection_y: Screen Y where the user clicked to choose a window.
            click_x: Screen X of the previously captured click point.
            click_y: Screen Y of the previously captured click point.

        Returns:
            The persisted binding payload (see ``create_binding_for_root``).

        Raises:
            WindowBindingError: When no other program's window is at the
                selection point, or root binding fails.
        """
        root_hwnd = self.root_window_at_point(
            selection_x, selection_y, exclude_current_process=True
        )
        if not root_hwnd:
            raise WindowBindingError(
                "没有选中其他程序窗口。请在提示出现后，左键单击目标程序窗口中的任意位置。"
            )
        return self.create_binding_for_root(root_hwnd, click_x, click_y)

    def create_binding(self, x: int, y: int) -> dict[str, Any]:
        """Compatibility helper for older callers that selected by click coordinate."""

        root_hwnd = self.root_window_at_point(x, y, exclude_current_process=True)
        if not root_hwnd:
            raise WindowBindingError(
                "当前坐标下没有找到其他程序的可绑定窗口。"
            )
        return self.create_binding_for_root(root_hwnd, x, y)

    def matches_binding(self, hwnd: int, binding: Mapping[str, Any]) -> bool:
        """Return True when *hwnd* still matches the identity stored in *binding*.

        Matching prefers the normalized process path, then the window title,
        and finally the PID, so a relaunched target can be re-found.
        """
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        expected_class = str(binding.get("root_class", "")).strip()
        if expected_class and self.class_name(hwnd) != expected_class:
            return False
        pid, process_path = self.process_info(hwnd)
        expected_path = os.path.normcase(str(binding.get("process_path", "")).strip())
        if expected_path:
            return process_path == expected_path
        expected_title = str(binding.get("root_title", "")).strip()
        if expected_title:
            return self.title(hwnd) == expected_title
        return int(binding.get("pid", 0) or 0) == pid

    def resolve_binding(
        self, binding: Mapping[str, Any]
    ) -> tuple[int, int, int] | None:
        """Resolve a saved binding to a live target window and client point.

        The stored root HWND is reused when it still matches; otherwise all
        visible top-level windows are scored and the best match is used. The
        stored target child is preferred, then the dialog control ID, and
        finally a fresh coordinate-based search.

        Args:
            binding: Persisted binding payload from ``create_binding_*``.

        Returns:
            ``(target_hwnd, client_x, client_y)`` for the resolved control,
            or ``None`` when the binding can no longer be satisfied.
        """
        if not isinstance(binding, dict) or not binding:
            return None
        try:
            stored_root = int(binding.get("root_hwnd", 0))
        except (TypeError, ValueError):
            stored_root = 0
        root_hwnd = stored_root if stored_root and self.matches_binding(stored_root, binding) else None
        if not root_hwnd:
            candidates: list[tuple[int, int]] = []
            expected_title = str(binding.get("root_title", "")).strip()
            expected_path = os.path.normcase(str(binding.get("process_path", "")).strip())

            def enum_proc(hwnd: int, _lparam: int) -> bool:
                if not user32.IsWindowVisible(hwnd) or not self.matches_binding(hwnd, binding):
                    return True
                pid, path = self.process_info(hwnd)
                score = (8 if expected_path and path == expected_path else 0)
                score += 4 if expected_title and self.title(hwnd) == expected_title else 0
                score += 2 if int(binding.get("pid", 0) or 0) == pid else 0
                candidates.append((score, int(hwnd)))
                return True

            try:
                callback = WNDENUMPROC(enum_proc)
                user32.EnumWindows(callback, 0)
            except Exception as exc:
                logger.warning("枚举窗口失败: %s", exc, exc_info=True)
                candidates = []
            if not candidates:
                return None
            candidates.sort(key=lambda item: item[0], reverse=True)
            root_hwnd = candidates[0][1]

        try:
            stored_target = int(binding.get("target_hwnd", 0))
        except (TypeError, ValueError):
            stored_target = 0
        target_class = str(binding.get("target_class", "")).strip()
        target_hwnd = None
        if stored_target and user32.IsWindow(stored_target):
            target_root = user32.GetAncestor(stored_target, GA_ROOT) or stored_target
            if int(target_root) == int(root_hwnd) and (
                not target_class or self.class_name(stored_target) == target_class
            ):
                target_hwnd = stored_target
        if not target_hwnd:
            control_id = int(binding.get("target_control_id", 0) or 0)
            if control_id > 0:
                candidate = user32.GetDlgItem(root_hwnd, control_id)
                if candidate and (not target_class or self.class_name(candidate) == target_class):
                    target_hwnd = candidate
        if target_hwnd:
            client_x = int(binding.get("target_client_x", 0) or 0)
            client_y = int(binding.get("target_client_y", 0) or 0)
        else:
            screen_point = POINT(
                int(binding.get("root_client_x", 0) or 0),
                int(binding.get("root_client_y", 0) or 0),
            )
            if not user32.ClientToScreen(root_hwnd, ctypes.byref(screen_point)):
                return None
            target_hwnd = self.background_click_target(
                screen_point.x, screen_point.y, expected_root=root_hwnd
            )
            if not target_hwnd:
                return None
            target_point = POINT(screen_point.x, screen_point.y)
            if not user32.ScreenToClient(target_hwnd, ctypes.byref(target_point)):
                return None
            client_x, client_y = int(target_point.x), int(target_point.y)
        client_rect = RECT()
        if not user32.GetClientRect(target_hwnd, ctypes.byref(client_rect)):
            return None
        if not (
            client_rect.left <= client_x < client_rect.right
            and client_rect.top <= client_y < client_rect.bottom
        ):
            return None
        return target_hwnd, client_x, client_y

    @staticmethod
    def binding_for_coordinate(
        binding: Mapping[str, Any], x: float, y: float
    ) -> dict[str, Any]:
        """Move a saved click binding by the source-coordinate delta.

        Args:
            binding: Persisted binding payload that carries ``source_x`` and
                ``source_y`` reference coordinates.
            x: New source X coordinate the click should apply to.
            y: New source Y coordinate the click should apply to.

        Returns:
            An adjusted copy of the binding with shifted root client
            coordinates; an empty dict when the binding lacks source
            coordinates or holds non-numeric values. Cached target-window
            fields are dropped whenever the click actually moved.
        """
        if not isinstance(binding, dict) or not binding:
            return {}
        try:
            raw_source_x = binding.get("source_x")
            raw_source_y = binding.get("source_y")
            if raw_source_x is None or raw_source_y is None:
                return {}
            source_x = int(raw_source_x)
            source_y = int(raw_source_y)
            target_x = int(round(float(x)))
            target_y = int(round(float(y)))
            delta_x = target_x - source_x
            delta_y = target_y - source_y
            root_x = int(binding.get("root_client_x", 0)) + delta_x
            root_y = int(binding.get("root_client_y", 0)) + delta_y
        except (TypeError, ValueError, OverflowError):
            return {}
        adjusted = dict(binding)
        adjusted["root_client_x"] = root_x
        adjusted["root_client_y"] = root_y
        adjusted["source_x"] = target_x
        adjusted["source_y"] = target_y
        if delta_x or delta_y:
            for key in (
                "target_hwnd",
                "target_class",
                "target_control_id",
                "target_client_x",
                "target_client_y",
            ):
                adjusted.pop(key, None)
        return adjusted

    @staticmethod
    def foreground_click(
        x: int,
        y: int,
        button: str,
        click_times: int,
        restore_position: bool = False,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Click ``(x, y)`` with the real mouse via pyautogui.

        Args:
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            button: Mouse button name, ``"left"`` or ``"right"``.
            click_times: Number of down/up pairs to send.
            restore_position: Move the cursor back to its original position
                afterwards.
            stop_requested: Optional emergency-stop predicate polled between
                clicks; returning True aborts the remaining repeats.
        """
        # stop_requested: 可选急停检查回调，由调用方传入。在多次点击循环中
        # 每次点击后检查，若返回 True 则提前中断循环。
        old_position = None
        if restore_position:
            try:
                old_position = pyautogui.position()
            except Exception as exc:
                logger.warning("获取当前鼠标位置失败: %s", exc, exc_info=True)
                old_position = None
        try:
            pyautogui.moveTo(x, y, duration=0)
            for _index in range(click_times):
                pressed = False
                try:
                    pyautogui.mouseDown(button=button)
                    pressed = True
                    time.sleep(MAPPING_POLL_INTERVAL_S)
                finally:
                    if pressed:
                        pyautogui.mouseUp(button=button)
                if stop_requested is not None and stop_requested():
                    break
                if click_times > 1:
                    time.sleep(POLL_INTERVAL_SLOW_S)
        finally:
            if restore_position and old_position:
                try:
                    pyautogui.moveTo(old_position.x, old_position.y, duration=0)
                except (AttributeError, TypeError):
                    pyautogui.moveTo(old_position[0], old_position[1], duration=0)

    @staticmethod
    def screen_point_for_target(
        hwnd: int, client_x: int, client_y: int
    ) -> tuple[int, int] | None:
        """Convert a target window's client point to screen coordinates.

        Returns:
            ``(screen_x, screen_y)``, or ``None`` when ClientToScreen fails.
        """
        point = POINT(int(client_x), int(client_y))
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def background_click(
        self,
        binding: Mapping[str, Any],
        button: str,
        click_times: int,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> bool:
        """Deliver a click to a bound control without activating the window.

        A single left click first attempts UI Automation activation; on
        failure it falls back to posting WM_*BUTTON* messages. Multi-clicks
        and right clicks go straight to PostMessage. The delivery method is
        recorded on ``last_background_method``/``last_background_detail``.

        Args:
            binding: Persisted binding payload to resolve.
            button: ``"left"`` or ``"right"``.
            click_times: Number of clicks to deliver.
            stop_requested: Optional emergency-stop predicate; when it
                returns True mid-sequence a pending button-up is still posted
                so the target is not left in a stuck state.

        Returns:
            True when the chosen delivery mechanism reported success. A
            timed-out UIA call returns False without a PostMessage retry
            because its outcome is unknown.
        """
        # UIA and PostMessage report delivery only; a target may still ignore either.
        self.last_background_method = ""
        self.last_background_detail = {}
        resolved = self.resolve_binding(binding)
        if not resolved:
            return False
        hwnd, client_x, client_y = resolved
        root_hwnd = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
        screen_point = self.screen_point_for_target(hwnd, client_x, client_y)
        if button == "left" and int(click_times) == 1 and screen_point:
            activate_kwargs: dict[str, Any] = {"preferred_hwnd": hwnd}
            if stop_requested is not None:
                activate_kwargs["stop_requested"] = stop_requested
            uia_result = self.ui_automation.activate(
                root_hwnd,
                screen_point[0],
                screen_point[1],
                **activate_kwargs,
            )
            self.last_background_detail = dict(uia_result)
            if uia_result.get("success"):
                self.last_background_method = str(
                    uia_result.get("method") or "UI Automation"
                )
                return True
            if uia_result.get("outcome_unknown"):
                # SAFETY: a timed-out COM call may still finish after the caller
                # resumes. Do not risk a duplicate action via PostMessage.
                self.last_background_method = "UI Automation 超时（未重复点击）"
                return False

        lparam = make_mouse_lparam(client_x, client_y)
        if button == "right":
            down_msg, up_msg, double_msg, down_flag = (
                WM_RBUTTONDOWN,
                WM_RBUTTONUP,
                WM_RBUTTONDBLCLK,
                MK_RBUTTON,
            )
        else:
            down_msg, up_msg, double_msg, down_flag = (
                WM_LBUTTONDOWN,
                WM_LBUTTONUP,
                WM_LBUTTONDBLCLK,
                MK_LBUTTON,
            )
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)  # best-effort, 不参与 ok
        ok = True
        if click_times >= 2:
            sequence = [
                (down_msg, down_flag),
                (up_msg, 0),
                (double_msg, down_flag),
                (up_msg, 0),
            ]
            # A double-click message pair only accounts for two clicks; append
            # extra DOWN/UP pairs so click_times > 2 delivers the full count.
            for _ in range(click_times - 2):
                sequence.extend([(down_msg, down_flag), (up_msg, 0)])
        else:
            sequence = [(down_msg, down_flag), (up_msg, 0)]
        down_sent = False
        for message, parameter in sequence:
            if stop_requested is not None and stop_requested():
                # SAFETY: avoid leaving the target window in a stuck
                # button-down state when the user triggers an emergency stop
                # between DOWN and UP messages.
                if down_sent:
                    user32.PostMessageW(hwnd, up_msg, 0, lparam)
                return False
            ok = bool(user32.PostMessageW(hwnd, message, parameter, lparam)) and ok
            down_sent = message != up_msg
            time.sleep(
                _POST_MESSAGE_INTERVAL_AFTER_UP_S
                if message == up_msg
                else _POST_MESSAGE_INTERVAL_S
            )
        if ok:
            self.last_background_method = "PostMessage"
        return ok

    def uia_control_action(
        self,
        binding: Mapping[str, Any],
        operation: str,
        value: str = "",
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run one UIA action against the control selected by a saved binding.

        Args:
            binding: Persisted binding payload to resolve.
            operation: One of ``"activate"``, ``"set_value"`` or
                ``"read_value"``.
            value: Text written by ``"set_value"``; ignored otherwise.
            stop_requested: Optional emergency-stop predicate forwarded to
                the UIA worker.

        Returns:
            The UI Automation result dict (``success``, ``method``, optional
            ``value`` and diagnostic fields).

        Raises:
            ValueError: When *operation* is not a supported action.
        """
        resolved = self.resolve_binding(binding)
        if not resolved:
            return {
                "available": self.ui_automation.available,
                "actionable": False,
                "success": False,
                "error": "目标窗口已经失效，请重新选择控件。",
            }
        hwnd, client_x, client_y = resolved
        root_hwnd = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
        point = self.screen_point_for_target(hwnd, client_x, client_y)
        if not point:
            return {
                "available": self.ui_automation.available,
                "actionable": False,
                "success": False,
                "error": "无法换算控件坐标。",
            }
        if operation == "activate":
            return self.ui_automation.activate(
                root_hwnd,
                point[0],
                point[1],
                preferred_hwnd=hwnd,
                stop_requested=stop_requested,
            )
        if operation == "set_value":
            return self.ui_automation.set_value(
                root_hwnd,
                point[0],
                point[1],
                str(value),
                preferred_hwnd=hwnd,
                stop_requested=stop_requested,
            )
        if operation == "read_value":
            return self.ui_automation.read_value(
                root_hwnd,
                point[0],
                point[1],
                preferred_hwnd=hwnd,
                stop_requested=stop_requested,
            )
        raise ValueError(f"不支持的 UI Automation 操作：{operation}")

    def inspect_binding(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Build a diagnostic report for a saved binding.

        Combines the WindowInspector's window/control details with a UI
        Automation probe of the resolved target so the UI can explain why a
        binding does or does not work.

        Returns:
            A dict with at least a ``valid`` flag; an invalid binding carries
            a ``warnings`` list instead of full details.
        """
        resolved = self.resolve_binding(binding)
        if not resolved:
            return {"valid": False, "warnings": ["目标窗口已经失效，请重新选择窗口。"]}
        target_hwnd, client_x, client_y = resolved
        root_hwnd = user32.GetAncestor(target_hwnd, GA_ROOT) or target_hwnd
        point = self.screen_point_for_target(target_hwnd, client_x, client_y)
        uia_report = (
            self.ui_automation.probe(
                root_hwnd,
                point[0],
                point[1],
                preferred_hwnd=target_hwnd,
            )
            if point
            else {
                "available": self.ui_automation.available,
                "actionable": False,
                "error": "无法换算绑定坐标。",
            }
        )
        return self.inspector.inspect(
            root_hwnd,
            target_hwnd,
            binding=cast(dict[str, Any], binding),
            uia_report=uia_report,
        )
