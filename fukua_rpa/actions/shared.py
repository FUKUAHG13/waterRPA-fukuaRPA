# pyright: reportAttributeAccessIssue=false
"""Shared action primitives, config, and registration table.

Mechanical extraction from the former monolithic ``engine_actions.py``.
Behavior lives in the sibling mixin modules; import it standalone only to
reach ``ACTION_HANDLERS`` / ``action_handler`` / the dataclasses.
"""

import logging
import math
import os
import random
from ctypes import byref, wintypes
from dataclasses import dataclass

from fukua_rpa.win32_api import GA_ROOT, POINT, kernel32, user32
from fukua_rpa.window_diagnostics import process_integrity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepActionContext:
    """Aggregated per-step action context passed to registered action handlers."""

    command: float
    value: object
    retry: int
    step_info: dict
    cache_key: str
    confidence: float
    use_gray: bool
    point_limit_enabled: bool = False
    point_limit_count: int = 0
    coordinate_step: dict | None = None
    image_click: dict | None = None
    task: dict | None = None
    coordinate_sequence: dict | None = None
    search_regions: list | None = None
    # 步骤级抖动覆盖。None 表示沿用全局抖动；非 None 时仅本步骤的点击/拖拽
    # 执行路径使用局部值，避免临时改写实例属性与附属点击线程产生竞态。
    move_jitter: float | None = None
    hold_jitter: float | None = None
    # 步骤级点击形态覆盖。spatial_jitter/restore_position 仅用于点击，
    # curve_strength 可用于点击和拖拽；None / False 表示沿用全局设置。
    spatial_jitter: float | None = None
    curve_strength: float | None = None
    # None follows the global visual-trail setting; True/False is a per-step
    # override. This affects only UI feedback, never pointer coordinates.
    trail_enabled: bool | None = None
    restore_position: bool = False
    # 步骤级系统按键按住时长覆盖（秒）。None 表示沿用全局设置，避免
    # 临时改写实例属性与附属点击线程产生竞态。
    key_hold_duration: float | None = None

@dataclass
class ClickTarget:
    """点击目标参数。"""

    img_path: str
    click_times: int = 1
    button: str = "left"
    point_limit_en: bool = False
    point_limit_count: int = 0

@dataclass
class ClickTiming:
    """点击执行与时序参数。"""

    move_jitter: float | None = None
    hold_jitter: float | None = None
    spatial_jitter: float | None = None
    curve_strength: float | None = None
    trail_enabled: bool | None = None
    restore_position: bool = False
    image_click_config: object = None
    coord_step_config: object = None
    coord_sequence_config: object = None
    background_click_config: object = None
    auxiliary_click_config: object = None

@dataclass
class ClickRetry:
    """点击重试与识别参数。"""

    retry: int = 0
    confidence: float = 0.8
    use_gray: bool = True
    cache_key: object = None
    search_regions: object = None
    allow_inverted: bool = False

ACTION_HANDLERS = {}

def _parse_jitter_value(value):
    """Parse a per-step jitter range into a clamped float, or None if unset.

    Mirrors ``run_config._number`` clamping (0..10s). Returns ``None`` for
    blank/invalid input so callers can fall back to the global jitter.
    """

    try:
        v = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        logger.debug("抖动参数解析失败: %s", exc, exc_info=True)
        return None
    if v < 0.0:
        v = 0.0
    if v > 10.0:
        v = 10.0
    return v

def _parse_optional_float(value, maximum):
    """Parse a per-step optional numeric override, or None if unset/blank.

    Returns ``None`` for blank/invalid input so callers fall back to the global
    value; clamps negative to 0 and values above ``maximum`` to ``maximum``.
    """

    if value is None:
        return None
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        logger.debug("可选数值参数解析失败: %s", exc, exc_info=True)
        return None
    if not math.isfinite(v):
        return None
    if v < 0.0:
        v = 0.0
    if v > maximum:
        v = maximum
    return v


# Win32 常量：仅在本模块的点击辅助函数内使用，避免污染 win32_api 的公共命名空间。
_SW_RESTORE = 9  # ShowWindow：还原并激活最小化窗口


def _configure_attach_thread_input():
    """为本模块用到的 user32.AttachThreadInput 声明类型签名（尽力而为）。

    win32_api 未声明该函数；按任务约束这里就地配置而不修改公共模块。
    若不声明，ctypes 会按默认 c_int 处理参数/返回值，在 64 位下可能错误
    传递 BOOL 附加标志。配置失败（如安全软件拦截）时静默降级，调用方仍
    可尝试默认约定。
    """
    try:
        user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        user32.AttachThreadInput.restype = wintypes.BOOL
    except Exception as exc:  # noqa: BLE001
        logger.debug("配置 AttachThreadInput 签名失败: %s", exc, exc_info=True)


_configure_attach_thread_input()


def root_window_at_point(x, y):
    """返回屏幕坐标 (x, y) 处所属的顶层窗口句柄；无窗口或调用失败返回 0。

    通过 WindowFromPoint 取得坐标处最深的子窗口，再用 GetAncestor(GA_ROOT)
    上溯到顶层窗口。前台点击只有屏幕坐标、没有目标句柄，借此把坐标映射到
    一个可用于完整性级别比较与窗口激活的句柄。
    """
    try:
        hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return int(root or hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("获取坐标处窗口失败: %s", exc, exc_info=True)
        return 0


def uipi_blocks_input(target_hwnd):
    """判断目标窗口是否因 UIPI 而阻止本机输入注入。

    DESIGN: Windows 的用户界面特权隔离（UIPI）会在目标进程的完整性级别高于
    本进程时，静默丢弃 SendInput / mouse_event 注入的事件——点击“看起来执行了”
    却没有效果。这里复用 window_diagnostics.process_integrity 读取双方令牌完整
    性级别：仅当两者都可读且目标级别更高时判定为阻止。该结果只用于向用户告
    警，绝不阻断点击（见调用方）。

    SAFETY: 本函数位于点击热路径，任何 Win32 / ctypes 异常都必须被吞掉并返回
    不阻断结果，否则一次句柄读取失败就会让所有点击失败。

    Returns:
        (blocked, pid) 元组：blocked 为目标进程完整性更高、输入会被丢弃时为
        True；pid 为目标窗口所属进程 ID，供调用方按进程去重告警。任何失败或
        无法判断时返回 (False, 0)。
    """
    try:
        target_hwnd = int(target_hwnd or 0)
        if not target_hwnd:
            return False, 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(target_hwnd), byref(pid))
        if not pid.value:
            return False, 0
        target = process_integrity(pid.value)
        current = process_integrity(os.getpid())
        if not (target.get("available") and current.get("available")):
            return False, int(pid.value)
        blocked = int(target.get("rank", 0)) > int(current.get("rank", 0))
        return blocked, int(pid.value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("UIPI 检测失败: %s", exc, exc_info=True)
        return False, 0


def activate_window_for_click(hwnd):
    """尽力把目标窗口激活到前台，使随后的真实点击能被其接收。返回是否成功。

    DESIGN: 部分应用会检查 GetForegroundWindow()，忽略发给非前台窗口的点击。
    使用 ShowWindow(SW_RESTORE) 还原最小化窗口，再用 AttachThreadInput 把当前
    线程临时附加到前台窗口线程后调用 SetForegroundWindow，以绕过系统对后台
    SetForegroundWindow 的限制；调用完成后立即解除附加。

    SAFETY: AttachThreadInput 的附加状态必须成对解除，否则会影响目标线程的
    输入处理；这里用 try/finally 保证解除。整体为尽力而为，失败仅返回 False，
    调用方应继续完成点击而不是中止。
    """
    try:
        hwnd = int(hwnd or 0)
        if not hwnd:
            return False
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        user32.ShowWindow(wintypes.HWND(hwnd), _SW_RESTORE)
        fore_hwnd = user32.GetForegroundWindow()
        fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None) if fore_hwnd else 0
        this_thread = kernel32.GetCurrentThreadId()
        attached = False
        if fore_thread and fore_thread != this_thread:
            attached = bool(user32.AttachThreadInput(fore_thread, this_thread, True))
        try:
            return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))
        finally:
            if attached:
                user32.AttachThreadInput(fore_thread, this_thread, False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("激活目标窗口失败: %s", exc, exc_info=True)
        return False


def action_handler(*command_codes):
    """Register a handler function for one or more command codes.

    Args:
        *command_codes: Numeric command codes mapped to the decorated handler.

    Returns:
        Decorator that registers the function and returns it unchanged.
    """

    def register(function):
        """Register the wrapped function under each normalized command code."""
        for code in command_codes:
            normalized = float(code)
            if normalized in ACTION_HANDLERS:
                raise RuntimeError(f"Duplicate action handler for command {normalized}")
            ACTION_HANDLERS[normalized] = function
        return function

    return register

def registered_action_codes():
    """Return the frozenset of command codes that have registered handlers."""
    return frozenset(ACTION_HANDLERS)

class ActionsSharedMixin:
    """Shared primitives, config, and registration."""

    _INPUT_SLICE_SECONDS = 0.025
    _MAX_ACTION_DURATION_SECONDS = 300.0
    _MAX_WAIT_DURATION_SECONDS = 86_400.0
    _MAX_ABS_COORDINATE = 1_000_000
    _WHEEL_DELTA = 120

    @classmethod
    def _bounded_action_duration(cls, value, *, default=0.0, maximum=None):
        """Parse and clamp an action duration to [0, maximum] seconds.

        Args:
            value: Raw duration input.
            default: Value returned when parsing fails.
            maximum: Upper bound; defaults to _MAX_ACTION_DURATION_SECONDS.

        Returns:
            The clamped duration as a float.

        Raises:
            ValueError: If the value is non-finite, negative, or above maximum.
        """
        maximum = cls._MAX_ACTION_DURATION_SECONDS if maximum is None else float(maximum)
        try:
            duration = float(value)
        except (TypeError, ValueError) as exc:
            logger.debug("动作时长解析失败: %s", exc, exc_info=True)
            return float(default)
        if not math.isfinite(duration) or duration < 0.0 or duration > maximum:
            raise ValueError(f"动作时长必须是 0 到 {maximum:g} 秒之间的有限数字")
        return duration

    @classmethod
    def _safe_input_coordinate(cls, value):
        """Validate and round an input coordinate to an in-range integer.

        Args:
            value: Raw coordinate input.

        Returns:
            The rounded coordinate as an int.

        Raises:
            ValueError: If the coordinate is non-finite or out of range.
        """
        coordinate = float(value)
        if not math.isfinite(coordinate) or abs(coordinate) > cls._MAX_ABS_COORDINATE:
            raise ValueError("鼠标坐标超出允许范围")
        return int(round(coordinate))

    def _tracked_mouse_down(self, button):
        """Press a mouse button and track it for later release.

        Args:
            button: Mouse button name ("left", "right", or "middle").

        Returns:
            True if pressed; False if a stop was requested.
        """
        normalized = str(button or "left").lower()
        with self._injected_input_lock:
            if self.check_stop_flag():
                return False
            self._injected_mouse_buttons.add(normalized)
            # DESIGN (RUN02): 先上报后按下，与 _tracked_key_down 对称。若先物理按下再
            # 上报，强杀（TerminateProcess 不跑 Python finally）窗口内宿主尚未收到
            # input_state，`_release_tracked_inputs` 无法代释该按钮 → 鼠标键残留。
            # 翻转顺序后窗口变为「宿主已知但物理未按」：强杀时宿主多发一次 mouseUp，
            # 对未按住的键无副作用（与 _tracked_mouse_up 的过度释放语义对称）。
            # 失败路径必须再次上报撤销，避免「宿主以为按住、实际未按」的错位。
            # 边界：已按住的按钮重复 _tracked_mouse_down 且失败时，回滚会移除该按钮
            # 并上报空；若首次按下物理仍生效且清理 mouse_up 未释放，宿主将不知该按钮。
            # 这是 run 级 cleanup（release_injected_inputs）仍可兜底的保守取舍。
            self._notify_injected_input_state()
            try:
                self.input_backend.mouse_down(button=normalized)
            except Exception:
                try:
                    self.input_backend.mouse_up(button=normalized)
                except Exception:
                    # Keep the tracked state so stop/finally can retry release.
                    raise
                self._injected_mouse_buttons.discard(normalized)
                self._notify_injected_input_state()
                raise
        return True

    def _tracked_mouse_up(self, button):
        """Release a previously tracked mouse button.

        Args:
            button: Mouse button name ("left", "right", or "middle").

        Returns:
            True if released; False if the button was not tracked as pressed.
        """
        normalized = str(button or "left").lower()
        with self._injected_input_lock:
            if normalized not in self._injected_mouse_buttons:
                return False
            try:
                self.input_backend.mouse_up(button=normalized)
            except Exception:
                # Do not forget a possibly pressed button; the run-level
                # cleanup gets another chance to release it.
                raise
            else:
                self._injected_mouse_buttons.discard(normalized)
                self._notify_injected_input_state()
        return True

    def _tracked_key_down(self, key):
        """Press a key and track it for later release.

        Args:
            key: Key name to press.

        Returns:
            True if pressed; False if a stop was requested.

        Raises:
            ValueError: If the key name is empty.
        """
        normalized = str(key or "").strip().lower()
        if not normalized:
            raise ValueError("按键名称不能为空")
        with self._injected_input_lock:
            if self.check_stop_flag():
                return False
            if normalized not in self._injected_keys:
                self._injected_keys.append(normalized)
            # DESIGN (RUN02): 先上报后按下。若先物理按下再上报，强杀（TerminateProcess
            # 不跑 Python finally）窗口内宿主尚未收到 input_state，`_release_tracked_inputs`
            # 无法代释该键 → 按键残留。翻转顺序后窗口变为「宿主已知但物理未按」：强杀时
            # 宿主多发一次 keyUp，对未按住的键无副作用（与 _tracked_key_up 的过度释放语义
            # 对称）。失败路径必须再次上报撤销，避免「宿主以为按住、实际未按」的错位。
            # 边界：已按住的键重复 _tracked_key_down 且失败时，回滚会移除该键并上报空；
            # 若首次按下物理仍生效且清理 key_up 未释放，宿主将不知该键。这是 run 级
            # cleanup（release_injected_inputs）仍可兜底的保守取舍 —— 保持宿主认知与
            # _injected_keys 一致，避免出现「宿主以为按住、实际未按」的长期错位。
            self._notify_injected_input_state()
            try:
                self.input_backend.key_down(normalized)
            except Exception:
                try:
                    self.input_backend.key_up(normalized)
                except Exception:
                    raise
                self._injected_keys = [
                    item for item in self._injected_keys if item != normalized
                ]
                self._notify_injected_input_state()
                raise
        return True

    def _tracked_key_up(self, key):
        """Release a previously tracked key.

        Args:
            key: Key name to release.

        Returns:
            True if released; False if the key was not tracked as pressed.
        """
        normalized = str(key or "").strip().lower()
        with self._injected_input_lock:
            if normalized not in self._injected_keys:
                return False
            try:
                self.input_backend.key_up(normalized)
            except Exception:
                raise
            else:
                self._injected_keys = [
                    item for item in self._injected_keys if item != normalized
                ]
                self._notify_injected_input_state()
        return True

    def _notify_injected_input_state(self):
        """Notify the registered callback of currently held injected inputs."""
        callback = getattr(self, "callback_input_state", None)
        if callback is None:
            return
        try:
            callback(
                {
                    "mouse_buttons": sorted(self._injected_mouse_buttons),
                    "keys": list(self._injected_keys),
                }
            )
        except Exception as exc:
            logger.debug("输入状态通知回调失败: %s", exc, exc_info=True)

    def release_injected_inputs(self):
        """Release only inputs that this engine instance actually pressed.

        SAFETY: never send blanket key-up events for physical user input. The
        tracked sets are also exported to the Runner guard for crash cleanup.
        """

        with self._injected_input_lock:
            keys = list(reversed(self._injected_keys))
            buttons = list(self._injected_mouse_buttons)
            for key in keys:
                try:
                    self.input_backend.key_up(key)
                except Exception as exc:
                    logger.warning("按键释放失败（输入可能残留）: %s", exc, exc_info=True)
                    continue
                self._injected_keys = [
                    item for item in self._injected_keys if item != key
                ]
            for button in buttons:
                try:
                    self.input_backend.mouse_up(button=button)
                except Exception as exc:
                    logger.warning("鼠标释放失败（输入可能残留）: %s", exc, exc_info=True)
                    continue
                self._injected_mouse_buttons.discard(button)
            self._notify_injected_input_state()

    @staticmethod
    def _jitter_delay(range_seconds):
        """Return a random delay offset in [-range_seconds, +range_seconds], or 0 if disabled."""
        try:
            r = float(range_seconds or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug("抖动范围解析失败: %s", exc, exc_info=True)
            r = 0.0
        if r <= 0.0:
            return 0.0
        return random.uniform(-r, r)
