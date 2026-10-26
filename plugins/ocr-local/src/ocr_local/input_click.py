"""独立模式下本地点击（仅 Windows）。

注意：这是给 ``tools/ocr_cli.py`` 在「不依赖 fukuaRPA 宿主」时使用的
便利实现。在 fukuaRPA 插件集成模式（P3）下，真正的点击由宿主通过
``input.mouse`` 权限执行，以保证「可被急停取消、停止后不残留输入」的
约束——插件本身不应直接注入鼠标事件。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


# DESIGN: 使用 ctypes.Structure 正确定义 Win32 INPUT/MOUSEINPUT 结构体，
# 确保 x64 上 sizeof(INPUT)==40、字段对齐正确。旧实现使用 28 字节原始
# 缓冲区（x86 尺寸）加手工 memmove，在 x64 上所有字段偏移错位。
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


# INVARIANT: x64 上 sizeof(INPUT) 必须为 40 字节，否则 SendInput 行为未定义。
assert ctypes.sizeof(INPUT) == 40 or ctypes.sizeof(ctypes.c_void_p) != 8, (
    f"INPUT 结构体大小异常：{ctypes.sizeof(INPUT)}（x64 应为 40）"
)

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
# MOUSEEVENTF_VIRTUALDESK：坐标映射到整个虚拟桌面而非仅主屏。
MOUSEEVENTF_VIRTUALDESK = 0x4000

_INPUT_MOUSE = 0


def _make_mouse_input(flags: int, dx: int, dy: int) -> INPUT:
    """构造一个 INPUT_MOUSE 类型的 INPUT 结构体实例。"""
    inp = INPUT()
    inp.type = _INPUT_MOUSE
    inp.union.mi.dx = dx
    inp.union.mi.dy = dy
    inp.union.mi.mouseData = 0
    inp.union.mi.dwFlags = flags
    inp.union.mi.time = 0
    inp.union.mi.dwExtraInfo = None
    return inp


def _send_mouse_input(user32, flags: int, dx: int, dy: int) -> None:
    """发送单个鼠标 INPUT 事件。

    FR-20260812-OCR-3: 检查 SendInput 返回值，发送失败必须显式报错，
    不能静默继续（否则点击可能未生效但脚本认为已执行）。
    """
    inp = _make_mouse_input(flags, dx, dy)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        # 注意（独立审查 2026-08-12）：user32 由 ctypes.windll 创建（未
        # use_last_error），此处 get_last_error() 恒 0，不写入错误信息以免
        # 误导诊断；返回 0 表示事件被拦截（UIPI/权限）。
        raise OSError(
            f"SendInput 发送鼠标事件失败（返回 {sent}；可能被 UIPI 或权限拦截）"
        )


def _virtual_desktop_metrics(user32) -> tuple[int, int, int, int]:
    """返回虚拟桌面四元组 (x, y, width, height)。

    FR-20260812-OCR-1: click_at 与 move_to 必须共用同一归一化口径（虚拟桌面
    四元组 + MOUSEEVENTF_VIRTUALDESK），否则多屏负坐标下 move_to 归一化错误。
    """
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def click_at(x: float, y: float, *, delay: float = 0.05) -> None:
    """用 Win32 SendInput 在物理坐标 (x, y) 左键单击一次。"""
    # INVARIANT: click_at 刻意把光标停在点击点，不在点击后复位到原位置；调用方
    # 若需复位，应自行在点击前后记录并 move_to 回原位。历史上曾有一个从未被传入、
    # 也从未被实现的 move_back 死参数，已按 D8-002 移除。
    if sys.platform != "win32":
        raise RuntimeError("本地点击仅支持 Windows；插件集成模式请交由 fukuaRPA 宿主执行")

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    # 把物理坐标转换成 SendInput 需要的绝对归一化坐标（0..65535）。
    # 使用虚拟桌面四元组，确保多显示器（含负坐标副屏）下归一化正确。
    vx, vy, vw, vh = _virtual_desktop_metrics(user32)

    ox = int(round((x - vx) * 65535 / max(1, vw)))
    oy = int(round((y - vy) * 65535 / max(1, vh)))

    # 移动到目标（绝对坐标 + 虚拟桌面）
    _send_mouse_input(user32, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ox, oy)
    time.sleep(delay)
    # 按下
    _send_mouse_input(user32, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ox, oy)
    time.sleep(delay)
    # 抬起
    _send_mouse_input(user32, MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ox, oy)


def move_to(x: float, y: float) -> None:
    """仅移动鼠标到 (x, y)，不点击。"""
    if sys.platform != "win32":
        raise RuntimeError("本地移动仅支持 Windows")

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # FR-20260812-OCR-1: 与 click_at 使用相同的虚拟桌面归一化口径，确保多屏
    # 负坐标下移动落点正确（旧实现用主屏尺寸且缺 VIRTUALDESK 标志）。
    vx, vy, vw, vh = _virtual_desktop_metrics(user32)
    dx = int(round((x - vx) * 65535 / max(1, vw)))
    dy = int(round((y - vy) * 65535 / max(1, vh)))

    _send_mouse_input(
        user32, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx, dy
    )
