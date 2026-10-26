"""Read-only Win32 target inspection for background-click diagnostics."""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from .win32_api import GA_ROOT, RECT, WNDENUMPROC, get_window_title, kernel32, user32  # noqa: E402

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25

INTEGRITY_LEVELS = (
    (0x0000, "不受信任"),
    (0x1000, "低"),
    (0x2000, "中"),
    (0x3000, "高（管理员）"),
    (0x4000, "系统"),
    (0x5000, "受保护进程"),
)

STANDARD_CONTROL_CLASSES = {
    "button",
    "edit",
    "static",
    "combobox",
    "comboboxex32",
    "listbox",
    "syslistview32",
    "systreeview32",
    "msctls_trackbar32",
    "msctls_progress32",
    "toolbarwindow32",
    "rebarwindow32",
}

CUSTOM_SURFACE_MARKERS = (
    "chrome_renderwidgethosthwnd",
    "cef",
    "qt",
    "unreal",
    "unity",
    "directui",
    "windows.ui.core",
)


class SIDAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("label", SIDAndAttributes)]


def integrity_label_from_rid(rid: int | None) -> str:
    """Map a mandatory-integrity RID to a human-readable Chinese label.

    Args:
        rid: The relative identifier from the process token's integrity SID.

    Returns:
        A label such as "中", "高（管理员）", or "不受信任".
    """
    rid = max(0, int(rid or 0))
    label = INTEGRITY_LEVELS[0][1]
    for threshold, candidate in INTEGRITY_LEVELS:
        if rid < threshold:
            break
        label = candidate
    return label


def integrity_rank(rid: int | None) -> int:
    """Return the ordinal rank of an integrity RID for comparison.

    Higher rank means higher privilege. Used to detect UIPI blocking
    (target rank > current rank implies messages may be filtered).

    Args:
        rid: The relative identifier from the process token's integrity SID.

    Returns:
        Zero-based index into INTEGRITY_LEVELS.
    """
    rid = max(0, int(rid or 0))
    rank = 0
    for index, (threshold, _label) in enumerate(INTEGRITY_LEVELS):
        if rid < threshold:
            break
        rank = index
    return rank


def classify_control(class_name: str | None, permission_blocked: bool = False) -> dict[str, Any]:
    """Classify a window control for background-click compatibility.

    Args:
        class_name: Win32 window class name (case-insensitive).
        permission_blocked: Whether UIPI is expected to block messages.

    Returns:
        Dict with keys ``classification`` (Chinese summary),
        ``standard_win32_control`` (bool), and ``custom_surface`` (bool).
    """
    normalized = str(class_name or "").strip().lower()
    standard = normalized in STANDARD_CONTROL_CLASSES or normalized.startswith(
        "windowsforms"
    )
    custom_surface = any(marker in normalized for marker in CUSTOM_SURFACE_MARKERS)
    if permission_blocked:
        level = "可能被权限阻止"
    elif standard:
        level = "消息点击较可能有效"
    elif custom_surface:
        level = "自绘/画布控件，兼容性较低"
    else:
        level = "实验性，需要实际测试"
    return {
        "classification": level,
        "standard_win32_control": standard,
        "custom_surface": custom_surface,
    }


def _configure_security_apis():
    # Reconfigures ctypes argtypes/restype on every call instead of caching a
    # module-level snapshot. This is intentional: window_diagnostics is meant
    # to be self-contained, so it does not share or depend on prototype state
    # configured by other modules (avoids inter-module coupling on the shared
    # kernel32/advapi32 handles). The cost is negligible for diagnostic use.
    advapi32 = ctypes.windll.advapi32
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    except Exception as exc:
        logger.warning("配置安全 API 类型签名失败: %s", exc, exc_info=True)
    return advapi32


def _handle_is_open(handle):
    if handle is None:
        return False
    return bool(getattr(handle, "value", handle))


def process_integrity(pid: int) -> dict[str, Any]:
    """Query the mandatory integrity level of a process via its token.

    Args:
        pid: Target process identifier.

    Returns:
        Dict with ``available`` (bool), ``rid`` (int), ``rank`` (int,
        only when available), ``label`` (str), and optionally ``error``.
    """
    advapi32 = _configure_security_apis()
    process_handle = None
    token_handle = wintypes.HANDLE()
    try:
        process_handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not process_handle:
            return {"available": False, "rid": 0, "label": "无法读取"}
        if not advapi32.OpenProcessToken(
            process_handle, TOKEN_QUERY, ctypes.byref(token_handle)
        ):
            return {"available": False, "rid": 0, "label": "无法读取"}
        required = wintypes.DWORD(0)
        advapi32.GetTokenInformation(
            token_handle,
            TOKEN_INTEGRITY_LEVEL,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            return {"available": False, "rid": 0, "label": "无法读取"}
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token_handle,
            TOKEN_INTEGRITY_LEVEL,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            return {"available": False, "rid": 0, "label": "无法读取"}
        label = ctypes.cast(
            buffer, ctypes.POINTER(TokenMandatoryLabel)
        ).contents
        sid = label.label.sid
        count_pointer = advapi32.GetSidSubAuthorityCount(sid)
        if not count_pointer or count_pointer.contents.value <= 0:
            return {"available": False, "rid": 0, "label": "无法读取"}
        rid_pointer = advapi32.GetSidSubAuthority(
            sid, count_pointer.contents.value - 1
        )
        if not rid_pointer:
            return {"available": False, "rid": 0, "label": "无法读取"}
        rid = int(rid_pointer.contents.value)
        return {
            "available": True,
            "rid": rid,
            "rank": integrity_rank(rid),
            "label": integrity_label_from_rid(rid),
        }
    except Exception as error:
        return {
            "available": False,
            "rid": 0,
            "label": "无法读取",
            "error": str(error),
        }
    finally:
        if _handle_is_open(token_handle):
            try:
                kernel32.CloseHandle(token_handle)
            except Exception as exc:
                logger.warning("关闭令牌句柄失败: %s", exc, exc_info=True)
        if _handle_is_open(process_handle):
            try:
                kernel32.CloseHandle(process_handle)
            except Exception as exc:
                logger.warning("关闭进程句柄失败: %s", exc, exc_info=True)


class WindowInspector:
    """Read-only Win32 window hierarchy inspector for diagnostics.

    Enumerates child controls, resolves process/integrity information,
    and produces a structured compatibility report used by the
    background-click diagnostic panel.
    """

    def __init__(self, process_id: int | None = None) -> None:
        """Initialize the inspector.

        Args:
            process_id: PID of the fukuaRPA process for integrity
                comparison; defaults to the current process.
        """
        self.process_id = int(process_id or os.getpid())

    @staticmethod
    def class_name(hwnd: int) -> str:
        """Return the Win32 window class name for a handle."""
        buffer = ctypes.create_unicode_buffer(256)
        try:
            length = int(user32.GetClassNameW(hwnd, buffer, len(buffer)))
            return buffer.value[:length] if length > 0 else ""
        except Exception as exc:
            logger.warning("获取窗口类名失败: %s", exc, exc_info=True)
            return ""

    @staticmethod
    def title(hwnd: int) -> str:
        """Return the window title text for a handle."""
        try:
            return get_window_title(hwnd)
        except Exception as exc:
            logger.warning("获取窗口标题失败: %s", exc, exc_info=True)
            return ""

    @staticmethod
    def process_info(hwnd: int) -> dict[str, Any]:
        """Return owning process metadata (pid, name, exe path) for a window.

        Requires psutil for name/path; without it only pid is populated.
        """
        pid = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        except Exception as exc:
            logger.warning("获取窗口进程 ID 失败: %s", exc, exc_info=True)
            return {"pid": 0, "name": "", "path": ""}
        name = ""
        path = ""
        if HAS_PSUTIL and pid.value:
            try:
                process = psutil.Process(pid.value)
                name = process.name()
                path = os.path.normpath(process.exe())
            except Exception as exc:
                logger.warning("读取进程信息失败 (PID %s): %s", pid.value, exc, exc_info=True)
        return {"pid": int(pid.value), "name": name, "path": path}

    @staticmethod
    def _rect(hwnd: int, client: bool = False) -> list[int] | None:
        """Return [left, top, right, bottom] for a window or client rect."""
        rect = RECT()
        try:
            ok = (
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                if client
                else user32.GetWindowRect(hwnd, ctypes.byref(rect))
            )
            if not ok:
                return None
            return [
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            ]
        except Exception as exc:
            logger.warning("获取窗口矩形失败: %s", exc, exc_info=True)
            return None

    def window_info(self, hwnd: int, root_hwnd: int | None = None) -> dict[str, Any]:
        """Collect structural metadata for a single window handle.

        Args:
            hwnd: Target window handle.
            root_hwnd: Optional ancestor used to compute nesting depth.

        Returns:
            Dict with hwnd, parent, depth, class, title, control_id,
            visibility, enabled state, and bounding rectangles.
        """
        try:
            parent = int(user32.GetParent(hwnd) or 0)
        except Exception as exc:
            logger.warning("获取父窗口失败: %s", exc, exc_info=True)
            parent = 0
        depth = 0
        current = parent
        while current and root_hwnd and current != int(root_hwnd) and depth < 16:
            depth += 1
            try:
                current = int(user32.GetParent(current) or 0)
            except Exception as exc:
                logger.warning("遍历父窗口链失败: %s", exc, exc_info=True)
                break
        try:
            control_id = max(0, int(user32.GetDlgCtrlID(hwnd)))
        except Exception as exc:
            logger.warning("获取控件 ID 失败: %s", exc, exc_info=True)
            control_id = 0
        return {
            "hwnd": int(hwnd),
            "parent_hwnd": parent,
            "depth": depth,
            "class": self.class_name(hwnd),
            "title": self.title(hwnd),
            "control_id": control_id,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "enabled": bool(user32.IsWindowEnabled(hwnd)),
            "window_rect": self._rect(hwnd),
            "client_rect": self._rect(hwnd, client=True),
        }

    def enumerate_children_with_truncation(
        self, root_hwnd: int, max_items: int = 128
    ) -> tuple[list[dict[str, Any]], bool]:
        """Enumerate child windows and report whether the cap truncated the result.

        ``enumerate_children`` silently returned a short list when the cap was
        hit, so a caller could not tell "this window has 5 children" from "this
        window has 500 and we looked at 128". A diagnostic report that claims to
        describe the tree must be able to state that it is partial.

        Returns:
            ``(children, truncated)`` where ``truncated`` is True exactly when
            enumeration stopped because the cap was reached.
        """
        children: list[dict[str, Any]] = []
        truncated = False

        def enum_proc(hwnd, _lparam):
            nonlocal truncated
            if len(children) >= max_items:
                truncated = True
                return False
            children.append(self.window_info(hwnd, root_hwnd=root_hwnd))
            return True

        try:
            callback = WNDENUMPROC(enum_proc)
            user32.EnumChildWindows(root_hwnd, callback, 0)
        except Exception as exc:
            logger.warning("枚举子窗口失败: %s", exc, exc_info=True)
        return children, truncated

    def enumerate_children(self, root_hwnd: int, max_items: int = 128) -> list[dict[str, Any]]:
        """Enumerate child windows up to a cap, returning metadata dicts.

        Args:
            root_hwnd: Parent window whose children are enumerated.
            max_items: Stop after this many children to bound work.

        Returns:
            List of window_info dicts for each discovered child.
        """
        children, _truncated = self.enumerate_children_with_truncation(root_hwnd, max_items)
        return children

    def inspect(
        self,
        root_hwnd: int,
        target_hwnd: int,
        binding: dict[str, Any] | None = None,
        uia_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Produce a full diagnostic report for a target window.

        Combines window hierarchy, process integrity comparison, control
        classification, UI Automation status, and child enumeration into
        a single structured dict consumed by the diagnostics panel.

        Args:
            root_hwnd: Top-level window handle (resolved to GA_ROOT).
            target_hwnd: Specific control handle under inspection.
            binding: Optional saved UIA binding metadata.
            uia_report: Optional UI Automation scan result.

        Returns:
            Structured report dict with validity, hierarchy, integrity,
            compatibility, warnings, and truncated child list.
        """
        root_hwnd = int(user32.GetAncestor(root_hwnd, GA_ROOT) or root_hwnd)
        target_hwnd = int(target_hwnd or root_hwnd)
        process = self.process_info(root_hwnd)
        current_integrity = process_integrity(self.process_id)
        target_integrity = process_integrity(process.get("pid", 0))
        permission_blocked = bool(
            current_integrity.get("available")
            and target_integrity.get("available")
            and target_integrity.get("rank", 0) > current_integrity.get("rank", 0)
        )
        target = self.window_info(target_hwnd, root_hwnd=root_hwnd)
        compatibility = classify_control(
            target.get("class", ""), permission_blocked=permission_blocked
        )
        uia_report = dict(uia_report or {})
        if uia_report.get("actionable") and not permission_blocked:
            compatibility["classification"] = "UI Automation 可直接操作"
        warnings = []
        if permission_blocked:
            warnings.append(
                "目标程序权限高于 fukuaRPA，Windows UIPI 可能阻止后台消息；可尝试以管理员身份运行。"
            )
        if not target.get("visible", False):
            warnings.append("目标控件当前不可见或窗口已最小化。")
        if compatibility["custom_surface"] and not uia_report.get("actionable"):
            warnings.append(
                "目标看起来是自绘、浏览器画布或游戏表面，PostMessage 可能被忽略。"
            )
        # FR-20261008-907: 直接消费枚举返回的截断标志，不得用
        # len(children) >= 128 重算——恰好等于上限时枚举自然结束，重算会把
        # 边界误判为「已截断」，且 128 是 max_items 默认值的复制粘贴。
        children, children_truncated = self.enumerate_children_with_truncation(root_hwnd)
        return {
            "valid": bool(user32.IsWindow(root_hwnd) and user32.IsWindow(target_hwnd)),
            "root": self.window_info(root_hwnd, root_hwnd=root_hwnd),
            "target": target,
            "process": process,
            "integrity": {
                "fukuaRPA": current_integrity,
                "target": target_integrity,
                "target_higher": permission_blocked,
            },
            "compatibility": {
                **compatibility,
                "post_message_available": not permission_blocked,
                "ui_automation_status": (
                    "可用"
                    if uia_report.get("actionable")
                    else ("未找到可操作控件" if uia_report.get("available") else "不可用")
                ),
            },
            "ui_automation": uia_report,
            "binding": dict(binding or {}),
            "child_count": len(children),
            "children_truncated": children_truncated,
            "children": children,
            "warnings": warnings,
        }


#: 需要折叠为单行的字符：CR/LF，加上 Unicode 强制/行边界字符。
#: FR-20260930-011：U+2028/U+2029/U+0085/\v/\f/\x1c-\x1e 也必须折叠，否则被检查
#: 程序可用它们伪造报告行，污染诊断输出。
_LINE_SEPARATOR_CHARS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _single_line(value: object) -> str:
    """Fold every line separator into a single space.

    FR-SCOPE015D-003：统一助手，禁止回到逐字段手工 ``replace``。
    逐字符行为保持不变（每个 ``\r`` 和每个 ``\n`` 各变成一个空格，因此
    ``"a\r\nb"`` 仍是 ``"a  b"``），以免改变既有输出；新增的只是此前漏掉的
    那批分隔符。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    for ch in _LINE_SEPARATOR_CHARS:
        if ch in text:
            text = text.replace(ch, " ")
    return text


def format_window_inspection(report: dict[str, Any] | None) -> str:
    """Format a structured inspection report as multi-line Chinese text.

    Args:
        report: The dict returned by :meth:`WindowInspector.inspect`,
            or None/invalid to produce a "window lost" message.

    Returns:
        Human-readable diagnostic text for display in the UI panel.
    """
    if not isinstance(report, dict) or not report.get("valid"):
        return "目标窗口已经失效，请重新选择窗口。"
    root = report.get("root", {})
    target = report.get("target", {})
    process = report.get("process", {})
    integrity = report.get("integrity", {})
    compatibility = report.get("compatibility", {})
    uia = report.get("ui_automation", {})
    uia_control = uia.get("control", {})
    if not uia.get("actionable"):
        uia_selection = "未选择"
    elif uia.get("matched_bound_hwnd"):
        uia_selection = "已绑定控件"
    else:
        uia_selection = "坐标处最深控件"
    lines = [
        f"兼容性判断：{compatibility.get('classification', '未知')}",
        f"进程：{_single_line(process.get('name')) or '未知'}  PID {process.get('pid', 0)}",
        f"路径：{_single_line(process.get('path')) or '无法读取'}",
        f"根窗口：HWND {root.get('hwnd', 0)}  类 {_single_line(root.get('class')) or '未知'}",
        f"标题：{_single_line(root.get('title')) or '（无标题）'}",
        f"目标控件：HWND {target.get('hwnd', 0)}  类 {_single_line(target.get('class')) or '未知'}  ID {target.get('control_id', 0)}",
        f"目标客户区：{target.get('client_rect')}",
        "UI Automation："
        f"{compatibility.get('ui_automation_status', '未知')}"
        + (
            f"，{_single_line(uia_control.get('control_type')) or '控件'} / "
            f"{_single_line(uia_control.get('action')) or '动作'}"
            if uia.get("actionable")
            else ""
        ),
        f"UIA 控件：{_single_line(uia_control.get('name')) or '（无名称）'}  "
        f"框架 {_single_line(uia_control.get('framework')) or '未知'}  "
        f"扫描 {uia.get('nodes_scanned', 0)} 项 / 候选 {uia.get('candidate_controls', 0)} 项  "
        f"选择 {uia_selection}",
        "权限：fukuaRPA "
        f"{integrity.get('fukuaRPA', {}).get('label', '未知')} / 目标 "
        f"{integrity.get('target', {}).get('label', '未知')}",
        f"枚举到子控件：{report.get('child_count', 0)} 个",
    ]
    for warning in report.get("warnings", []):
        lines.append(f"警告：{warning}")
    if uia.get("error") and not uia.get("actionable"):
        lines.append(f"UIA 说明：{_single_line(uia.get('error'))}")
    lines.append("")
    lines.append("子控件（最多 128 项）：")
    for child in report.get("children", []):
        indent = "  " * min(8, int(child.get("depth", 0)) + 1)
        title = _single_line(child.get("title", ""))
        lines.append(
            f"{indent}HWND {child.get('hwnd', 0)} | {_single_line(child.get('class')) or '未知'} | "
            f"ID {child.get('control_id', 0)} | {title or '（无文本）'}"
        )
    if report.get("children_truncated"):
        lines.append("……子控件数量超过上限，报告已截断。")
    return "\n".join(lines)
