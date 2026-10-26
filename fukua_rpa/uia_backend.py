"""Bounded UI Automation probing and background control activation."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto

    HAS_UI_AUTOMATION = True
    UI_AUTOMATION_IMPORT_ERROR = ""
except Exception as error:  # pragma: no cover - exercised by frozen self-test
    auto = None
    HAS_UI_AUTOMATION = False
    UI_AUTOMATION_IMPORT_ERROR = str(error)


MAX_UIA_NODES = 384
MAX_UIA_DEPTH = 12
UIA_REQUEST_TIMEOUT_SECONDS = 2.5
UIA_TREE_BUDGET_SECONDS = 1.5


def _bounded_text(value: Any, limit: int = 160) -> str:
    """Flatten *value* to a single-line string truncated to *limit* chars."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _point_in_rect(rect: Any, x: int, y: int) -> bool:
    """Return True when ``(x, y)`` lies inside *rect* (left/top inclusive)."""
    try:
        return (
            float(rect.left) <= int(x) < float(rect.right)
            and float(rect.top) <= int(y) < float(rect.bottom)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _rect_area(rect: Any) -> float:
    """Return the non-negative pixel area of a UIA bounding rectangle."""
    try:
        return max(0.0, float(rect.right) - float(rect.left)) * max(
            0.0, float(rect.bottom) - float(rect.top)
        )
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _control_details(control: Any, action: str, depth: int) -> dict[str, Any]:
    """Serialize a UIA control's identity and state for diagnostic reports."""
    def read(name: str, default: Any = "") -> Any:
        try:
            return getattr(control, name)
        except Exception as exc:
            logger.warning("读取控件属性 %s 失败: %s", name, exc, exc_info=True)
            return default

    rect = read("BoundingRectangle", None)
    bounds = None
    if rect is not None:
        try:
            bounds = [
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            ]
        except (AttributeError, TypeError, ValueError):
            bounds = None
    return {
        "name": _bounded_text(read("Name")),
        "automation_id": _bounded_text(read("AutomationId")),
        "class": _bounded_text(read("ClassName")),
        "framework": _bounded_text(read("FrameworkId")),
        "control_type": _bounded_text(read("ControlTypeName", "未知")),
        "enabled": bool(read("IsEnabled", False)),
        "offscreen": bool(read("IsOffscreen", False)),
        "native_hwnd": int(read("NativeWindowHandle", 0) or 0),
        "bounds": bounds,
        "depth": int(depth),
        "action": str(action),
    }


def _pattern_candidates(
    control: Any, operation: str = "activate"
) -> list[tuple[int, str, Any]]:
    """Return prioritized (priority, action_name, pattern) tuples for *control*."""
    candidates = []
    if operation in ("set_value", "read_value"):
        for priority, action, pattern_id in (
            (60, "Value", auto.PatternId.ValuePattern),
            (20, "LegacyValue", auto.PatternId.LegacyIAccessiblePattern),
        ):
            try:
                pattern = control.GetPattern(pattern_id)
                # REVIEW-20260913-001 FR-004: GetPattern 返回 None 表示控件不支持
                # 该模式，属正常语义而非错误。必须在此显式判空；否则下面的
                # pattern.IsReadOnly 会抛 AttributeError，被宽 except 捕获后输出
                # 含堆栈的误导性 WARNING（该调用对每个候选控件各执行一次）。
                if (
                    pattern is not None
                    and operation == "set_value"
                    and action == "Value"
                    and pattern.IsReadOnly
                ):
                    pattern = None
            except Exception as exc:
                logger.warning("获取 %s 模式失败: %s", action, exc, exc_info=True)
                pattern = None
            if pattern is not None:
                candidates.append((priority, action, pattern))
        return candidates
    for priority, action, pattern_id in (
        (40, "Invoke", auto.PatternId.InvokePattern),
        (30, "Selection", auto.PatternId.SelectionItemPattern),
        (20, "Toggle", auto.PatternId.TogglePattern),
        (10, "DefaultAction", auto.PatternId.LegacyIAccessiblePattern),
    ):
        try:
            pattern = control.GetPattern(pattern_id)
        except Exception as exc:
            logger.warning("获取 %s 模式失败: %s", action, exc, exc_info=True)
            pattern = None
        if pattern is not None:
            candidates.append((priority, action, pattern))
    return candidates


def _candidate_score(
    *,
    depth: int,
    area: float,
    action_priority: int,
    native_hwnd: int,
    preferred_hwnd: int,
    root_hwnd: int,
) -> tuple[int, int, float, int]:
    """Compute a sort key that prefers the bound child, then depth, then area."""
    exact_bound_control = int(
        preferred_hwnd
        and preferred_hwnd != root_hwnd
        and native_hwnd == preferred_hwnd
    )
    return (
        exact_bound_control,
        int(depth),
        -max(0.0, float(area)),
        int(action_priority),
    )


def _find_actionable_control(
    root_hwnd: int,
    x: int,
    y: int,
    cancelled: threading.Event | None = None,
    preferred_hwnd: int = 0,
    operation: str = "activate",
) -> dict[str, Any]:
    """Walk the UIA tree under *root_hwnd* and find the best control at a point.

    Returns:
        A result dict with ``actionable`` True and internal ``_pattern`` /
        ``_action`` keys when a usable control is found, or ``actionable``
        False with a diagnostic ``error`` otherwise.
    """
    if cancelled is not None and cancelled.is_set():
        return {
            "available": True,
            "actionable": False,
            "cancelled": True,
            "error": "UI Automation 请求已取消。",
        }
    root = auto.ControlFromHandle(int(root_hwnd))
    if root is None:
        return {"available": True, "actionable": False, "error": "无法读取绑定根窗口的 UI Automation 树。"}

    started = time.monotonic()
    scanned = 0
    candidate_controls = 0
    truncated = False
    matches = []
    try:
        iterator = auto.WalkControl(root, includeTop=True, maxDepth=MAX_UIA_DEPTH)
        for control, depth in iterator:
            if cancelled is not None and cancelled.is_set():
                return {
                    "available": True,
                    "actionable": False,
                    "cancelled": True,
                    "nodes_scanned": scanned,
                    "tree_truncated": truncated,
                    "error": "UI Automation 请求已取消。",
                }
            scanned += 1
            if scanned > MAX_UIA_NODES or time.monotonic() - started > UIA_TREE_BUDGET_SECONDS:
                truncated = True
                break
            try:
                rect = control.BoundingRectangle
            except Exception as exc:
                logger.warning("读取控件 BoundingRectangle 失败: %s", exc, exc_info=True)
                continue
            if not _point_in_rect(rect, x, y):
                continue
            try:
                if not control.IsEnabled:
                    continue
                if control.IsOffscreen:
                    continue
            except Exception as exc:
                # A control whose enabled/offscreen state cannot be read is in an
                # indeterminate state; skip it rather than treating it as a
                # matchable, visible, enabled target.
                logger.warning("读取控件可见性状态失败: %s", exc, exc_info=True)
                continue
            patterns = _pattern_candidates(control, operation)
            if not patterns:
                continue
            candidate_controls += 1
            try:
                native_hwnd = int(control.NativeWindowHandle or 0)
            except Exception as exc:
                logger.warning("读取控件原生窗口句柄失败: %s", exc, exc_info=True)
                native_hwnd = 0
            for priority, action, pattern in patterns:
                # An explicitly bound child HWND wins. Otherwise the deepest,
                # smallest control at the coordinate is safer than an ancestor.
                score = _candidate_score(
                    depth=int(depth),
                    area=_rect_area(rect),
                    action_priority=priority,
                    native_hwnd=native_hwnd,
                    preferred_hwnd=int(preferred_hwnd or 0),
                    root_hwnd=int(root_hwnd),
                )
                matches.append(
                    (score, control, action, pattern, int(depth), native_hwnd)
                )
    except Exception as error:
        return {
            "available": True,
            "actionable": False,
            "nodes_scanned": scanned,
            "candidate_controls": candidate_controls,
            "tree_truncated": truncated,
            "error": _bounded_text(error, 240),
        }

    if not matches:
        return {
            "available": True,
            "actionable": False,
            "nodes_scanned": scanned,
            "candidate_controls": candidate_controls,
            "tree_truncated": truncated,
            "error": (
                "坐标处没有发现可读取或设置文本的控件。"
                if operation in ("set_value", "read_value")
                else "坐标处没有发现支持 Invoke、Selection、Toggle 或默认动作的控件。"
            ),
        }
    matches.sort(key=lambda item: item[0], reverse=True)
    _score, control, action, pattern, depth, native_hwnd = matches[0]
    matched_bound_hwnd = bool(
        preferred_hwnd
        and preferred_hwnd != root_hwnd
        and native_hwnd == preferred_hwnd
    )
    return {
        "available": True,
        "actionable": True,
        "nodes_scanned": scanned,
        "candidate_controls": candidate_controls,
        "tree_truncated": truncated,
        "selection": "bound_hwnd" if matched_bound_hwnd else "deepest_control",
        "matched_bound_hwnd": matched_bound_hwnd,
        "control": _control_details(control, action, depth),
        "_pattern": pattern,
        "_action": action,
    }


#: 会改变目标状态的操作：中途失败时结果不可知，调用方不得重试。
_SIDE_EFFECTING_UIA_OPERATIONS = frozenset({"activate", "set_value"})


def _run_pattern(pattern: Any, action: str) -> bool | None:
    """Invoke a single UIA pattern action.

    Returns:
        ``True`` on success, ``False`` when the call completed and reported failure,
        and ``None`` when the call **died mid-flight** (COM marshalling/RPC error).
        ``None`` matters: the provider may already have performed the action, so
        the caller must not treat it as a clean failure and retry elsewhere — that
        retry is what produces a duplicate activation.
    """
    try:
        if action == "Invoke":
            return bool(pattern.Invoke(waitTime=0))
        if action == "Selection":
            return bool(pattern.Select(waitTime=0))
        if action == "Toggle":
            return bool(pattern.Toggle(waitTime=0))
        if action == "DefaultAction":
            return bool(pattern.DoDefaultAction(waitTime=0))
    except Exception as exc:
        logger.warning("执行控件动作 %s 中途失败，结果未知: %s", action, exc, exc_info=True)
        return None
    return False


def _perform_uia_job(
    operation: str,
    payload: dict[str, Any],
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute one UIA operation (probe/activate/set_value/read_value) in-thread."""
    result = _find_actionable_control(
        int(payload.get("root_hwnd", 0)),
        int(payload.get("x", 0)),
        int(payload.get("y", 0)),
        cancelled,
        int(payload.get("preferred_hwnd", 0)),
        operation,
    )
    pattern = result.pop("_pattern", None)
    action = result.pop("_action", "")
    if operation == "probe" or not result.get("actionable"):
        return result
    if pattern is None:
        return {**result, "success": False, "error": "未知的 UI Automation 操作。"}
    if cancelled is not None and cancelled.is_set():
        return {
            **result,
            "success": False,
            "cancelled": True,
            "error": "UI Automation 请求已取消，未执行控件动作。",
        }
    # INVARIANT: 会产生副作用的操作在 COM 中途失败时，结果是**未知**而非失败。
    # 调用方据此不得回退到 PostMessage 重试，否则会在用户屏幕上造成重复点击。
    outcome_unknown = False
    if operation == "activate":
        try:
            success = _run_pattern(pattern, action)
        except Exception as exc:
            logger.warning("UI Automation activate 调用失败，结果未知: %s", exc, exc_info=True)
            success = False
            outcome_unknown = True
        if success is None:
            # 中途失败：动作可能已生效，结果未知。
            success, outcome_unknown = False, True
        value = None
    elif operation == "set_value":
        try:
            try:
                success = bool(pattern.SetValue(str(payload.get("value", "")), waitTime=0))
            except TypeError:
                success = bool(pattern.SetValue(str(payload.get("value", ""))))
        except Exception as exc:
            logger.warning("UI Automation SetValue 调用失败，结果未知: %s", exc, exc_info=True)
            success = False
            outcome_unknown = True
        value = None
    elif operation == "read_value":
        try:
            value = str(pattern.Value)
            success = True
        except Exception as exc:
            # 纯读取：失败就是失败，不存在"可能已生效"，因此不标记结果未知。
            logger.warning("读取控件 Value 失败: %s", exc, exc_info=True)
            value = ""
            success = False
    else:
        success = False
        value = None
    payload_out = {
        **result,
        "success": success,
        "method": f"UI Automation {action}",
        "value": value,
        "error": "" if success else f"控件声明支持 {action}，但调用没有成功。",
    }
    if outcome_unknown:
        payload_out["outcome_unknown"] = True
        payload_out["error"] = f"UI Automation {action} 调用异常，动作可能已生效。"
    return payload_out


class _UIAutomationService:
    """Keep all COM objects in one bounded worker apartment."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=16)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._startup_error = UI_AUTOMATION_IMPORT_ERROR
        self._closing = False

    @property
    def available(self) -> bool:
        """True when the uiautomation package loaded and close() was not called."""
        return HAS_UI_AUTOMATION and not self._closing

    def _ensure_started(self) -> bool:
        """Lazily start the COM worker thread; return True when it is alive."""
        if not self.available:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._ready.clear()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker,
                daemon=True,
                name="fukuaRPA-uia",
            )
            self._thread.start()
        self._ready.wait(2.0)
        return bool(self._thread and self._thread.is_alive() and not self._startup_error)

    def _worker(self) -> None:
        """Initialize COM, process queued jobs, then uninitialize on exit."""
        initializer = None
        try:
            initializer = auto.UIAutomationInitializerInThread()
            auto.SetGlobalSearchTimeout(1.0)
            # Instantiate the COM client in this apartment before accepting work.
            auto.GetRootControl()
            self._startup_error = ""
        except Exception as error:
            self._startup_error = _bounded_text(error, 240)
        finally:
            self._ready.set()
        if self._startup_error:
            if initializer is not None:
                initializer.Uninitialize()
            return

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            operation, payload, completed, cancelled, holder = item
            try:
                holder["result"] = _perform_uia_job(operation, payload, cancelled)
            except Exception as error:
                holder["result"] = {
                    "available": True,
                    "actionable": False,
                    "success": False,
                    "error": _bounded_text(error, 240),
                }
                # INVARIANT: 会产生副作用的操作在工作线程里崩溃时，动作是否已经
                # 送达不可知。必须回报"结果未知"，调用方才不会回退到 PostMessage
                # 重试——那会在用户屏幕上造成重复点击。纯读取不涉及副作用。
                if operation in _SIDE_EFFECTING_UIA_OPERATIONS:
                    holder["result"]["outcome_unknown"] = True
            finally:
                completed.set()
        if initializer is not None:
            initializer.Uninitialize()

    def request(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Submit one UIA job and block until it completes, times out, or is cancelled.

        Args:
            operation: One of ``"probe"``, ``"activate"``, ``"set_value"``
                or ``"read_value"``.
            payload: Job parameters (root_hwnd, x, y, preferred_hwnd, value).
            stop_requested: Optional emergency-stop predicate polled while
                waiting; triggers cooperative cancellation.

        Returns:
            A result dict with at least ``available``, ``actionable`` and
            ``success`` keys plus diagnostic fields.
        """
        if not self._ensure_started():
            return {
                "available": False,
                "actionable": False,
                "success": False,
                "error": self._startup_error or "UI Automation 不可用。",
            }
        completed = threading.Event()
        cancelled = threading.Event()
        holder: dict[str, Any] = {}
        try:
            self._queue.put_nowait(
                (operation, dict(payload), completed, cancelled, holder)
            )
        except queue.Full:
            return {
                "available": True,
                "actionable": False,
                "success": False,
                "error": "UI Automation 请求队列繁忙。",
            }
        deadline = time.monotonic() + UIA_REQUEST_TIMEOUT_SECONDS
        while not completed.is_set():
            if stop_requested is not None and stop_requested():
                cancelled.set()
                return {
                    "available": True,
                    "actionable": False,
                    "success": False,
                    "cancelled": True,
                    "outcome_unknown": operation in ("activate", "set_value"),
                    "error": "UI Automation 操作已由急停取消。",
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancelled.set()
                # SAFETY: COM cancellation is cooperative. A timed-out write or
                # activation may still complete, so report an unknown outcome;
                # callers must stop instead of retrying another click backend.
                return {
                    "available": True,
                    "actionable": False,
                    "success": False,
                    "timed_out": True,
                    "outcome_unknown": operation in ("activate", "set_value"),
                    "error": "UI Automation 控件响应超时，动作结果未知。",
                }
            completed.wait(min(0.025, remaining))
        return dict(holder.get("result", {}))

    def close(self) -> None:
        """Signal the worker to stop and wait up to 1 s for it to exit."""
        self._closing = True
        thread = self._thread
        if not thread or not thread.is_alive():
            return
        deadline = time.monotonic() + 1.0
        while thread.is_alive():
            try:
                self._queue.put(None, timeout=0.1)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        self._stop_event.set()
        # DESIGN: worker 为 daemon 线程，不阻止进程退出。1s join 超时后若线程
        # 仍阻塞于 COM 调用，残留线程将随进程终止而销毁，无需额外清理。
        thread.join(timeout=1.0)


class UIAutomationBackend:
    """Thread-safe facade over the single-threaded UIA COM worker.

    All public methods marshal their request to the worker apartment and
    block the caller until the bounded timeout expires or the emergency
    stop fires.
    """

    def __init__(self, service: Any | None = None) -> None:
        self._service = service or _UIAutomationService()

    @property
    def available(self) -> bool:
        """True when the underlying UIA service is importable and not closed."""
        return bool(getattr(self._service, "available", False))

    def probe(
        self,
        root_hwnd: int,
        x: int,
        y: int,
        preferred_hwnd: int = 0,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Inspect the control at a point without performing any action.

        Args:
            root_hwnd: Root window whose UIA tree is searched.
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            preferred_hwnd: When non-zero, prefer the control whose native
                HWND matches this value.
            stop_requested: Optional emergency-stop predicate.

        Returns:
            A diagnostic result dict with control details when actionable.
        """
        kwargs: dict[str, Any] = {"stop_requested": stop_requested} if stop_requested else {}
        return self._service.request(
            "probe",
            {
                "root_hwnd": int(root_hwnd),
                "x": int(x),
                "y": int(y),
                "preferred_hwnd": int(preferred_hwnd),
            },
            **kwargs,
        )

    def activate(
        self,
        root_hwnd: int,
        x: int,
        y: int,
        preferred_hwnd: int = 0,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Invoke the default action (click) on the control at a point.

        Args:
            root_hwnd: Root window whose UIA tree is searched.
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            preferred_hwnd: When non-zero, prefer the control whose native
                HWND matches this value.
            stop_requested: Optional emergency-stop predicate.

        Returns:
            A result dict; ``success`` True when the pattern call succeeded.
        """
        kwargs: dict[str, Any] = {"stop_requested": stop_requested} if stop_requested else {}
        return self._service.request(
            "activate",
            {
                "root_hwnd": int(root_hwnd),
                "x": int(x),
                "y": int(y),
                "preferred_hwnd": int(preferred_hwnd),
            },
            **kwargs,
        )

    def set_value(
        self,
        root_hwnd: int,
        x: int,
        y: int,
        value: str,
        preferred_hwnd: int = 0,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Write text into the editable control at a point.

        Args:
            root_hwnd: Root window whose UIA tree is searched.
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            value: Text to write via the Value or LegacyIAccessible pattern.
            preferred_hwnd: When non-zero, prefer the control whose native
                HWND matches this value.
            stop_requested: Optional emergency-stop predicate.

        Returns:
            A result dict; ``success`` True when the pattern accepted the text.
        """
        kwargs: dict[str, Any] = {"stop_requested": stop_requested} if stop_requested else {}
        return self._service.request(
            "set_value",
            {
                "root_hwnd": int(root_hwnd),
                "x": int(x),
                "y": int(y),
                "preferred_hwnd": int(preferred_hwnd),
                "value": str(value),
            },
            **kwargs,
        )

    def read_value(
        self,
        root_hwnd: int,
        x: int,
        y: int,
        preferred_hwnd: int = 0,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Read the current text from the control at a point.

        Args:
            root_hwnd: Root window whose UIA tree is searched.
            x: Horizontal screen coordinate.
            y: Vertical screen coordinate.
            preferred_hwnd: When non-zero, prefer the control whose native
                HWND matches this value.
            stop_requested: Optional emergency-stop predicate.

        Returns:
            A result dict; on success the ``value`` key holds the text.
        """
        kwargs: dict[str, Any] = {"stop_requested": stop_requested} if stop_requested else {}
        return self._service.request(
            "read_value",
            {
                "root_hwnd": int(root_hwnd),
                "x": int(x),
                "y": int(y),
                "preferred_hwnd": int(preferred_hwnd),
            },
            **kwargs,
        )

    def close(self) -> None:
        """Shut down the UIA worker thread if the service supports it."""
        close = getattr(self._service, "close", None)
        if callable(close):
            close()
