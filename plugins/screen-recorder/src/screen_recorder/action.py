"""插件动作入口（fukuaRPA 插件契约实现）。

宿主进程（P3）加载插件后调用 ``run(request)``：request 是 PluginProtocol 的
request 信封，其 ``payload`` 内包含 ``action_id`` 与本步骤的 ``plugin_config``
（即用户填的参数）。本模块派发到具体录制动作，返回 PluginResponse 形状的
字典 ``{"ok", "result", "error"}``。

权限模型（见 manifest.json）：
* screen.capture_region —— 抓屏/录屏核心能力。
* files.user_selected —— 用户指定输出文件路径。
* files.plugin_private_data —— 未指定路径时写入插件私有目录（由宿主通过
  ``output_dir`` 提供，或用户在前端选择）。
* 本插件**不声明任何网络权限**：录屏/截图为纯本地行为，符合 fukuaRPA「不联网」。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from .backend import _BACKEND_CACHE, RecorderBackend, create_backend
from .engine import dispatch

# 支持的动作 id 白名单（与 manifest 的 actions 对齐）。
_ACTION_IDS = (
    "recorder_start",
    "recorder_pause",
    "recorder_resume",
    "recorder_stop",
    "recorder_snapshot",
)

# 扁平信封回退时需过滤的元字段（非动作参数）。
_META_KEYS = {"run_id", "step_id", "plugin_id", "action_id", "deadline_ns"}


# INVARIANT: 后端缓存受 _BACKEND_LOCK 保护，P3 并发宿主下安全。
# 单例后端本身仍非线程安全（MssOpencvBackend/RecorderBackend 的录制会话在 start() 时可能关闭旧会话），
# P3 若需真正并发执行同一插件的多个步骤，须改为每请求独立后端。
_BACKEND_LOCK = threading.Lock()


def _get_backend(name: str = "mss") -> RecorderBackend:
    with _BACKEND_LOCK:
        cached = _BACKEND_CACHE.get(name)
        if cached is None:
            cached = create_backend(name)
            _BACKEND_CACHE[name] = cached
        return cached


def _response(ok: bool, result: Any, error: str | None = None,
              run_id: str | None = None, step_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "result": result, "error": error}
    # 协议强制字段 run_id/step_id：仅当调用方（run）从信封取出并提供时才回显，
    # 以兼容不携带信封的逻辑核心 execute_action / CLI（修复 R-recorder-id）。
    if run_id is not None:
        out["run_id"] = run_id
    if step_id is not None:
        out["step_id"] = step_id
    return out


def execute_action(
    plugin_id: str,
    action_id: str,
    payload: dict[str, Any],
    *,
    backend: RecorderBackend | None = None,
    backend_name: str = "mss",
    auto_close: bool = False,
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    """执行一个录制动作，返回 PluginResponse 形状的字典。"""

    if action_id not in _ACTION_IDS:
        return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)

    own_backend = backend is None
    if own_backend:
        try:
            backend = _get_backend(backend_name)
        except Exception as exc:  # noqa: BLE001
            return _response(False, None, f"无法创建录制后端：{exc}", run_id=run_id, step_id=step_id)

    try:
        data = dispatch(backend, action_id, payload or {})
        return _response(True, data, run_id=run_id, step_id=step_id)
    except Exception as exc:  # noqa: BLE001
        return _response(False, None, f"录制动作执行失败：{exc}", run_id=run_id, step_id=step_id)
    finally:
        # FR-a01-001（与 browser-automation 的 FR-20260813-D11-001 同型修复）：只要
        # 调用方显式要求 auto_close，就必须关闭 backend，不论是否由本函数创建
        # （own_backend）。此前 own_backend 守卫使 CLI 自建并传入的 backend（未入
        # _BACKEND_CACHE）在 auto_close=True 时漏关、逃过 atexit，造成 ffmpeg 子进程
        # 泄漏。run() 宿主路径不传 auto_close，不受影响。
        if auto_close and backend is not None:
            with contextlib.suppress(Exception):
                backend.close()


def run(
    request: dict[str, Any],
    *,
    backend: RecorderBackend | None = None,
    backend_name: str = "mss",
) -> dict[str, Any]:
    """宿主进程（P3）调用入口。入参为 PluginProtocol 的 request 信封。

    ``backend`` / ``backend_name`` 由宿主注入；默认走 mss（opencv）后端。
    宿主若要跨动作复用同一录制会话，应把实例作为 ``backend`` 传入。
    """

    # 信封里的 run_id/step_id 需回显到响应（plugin_protocol 响应分支强制）。
    # 预初始化为 None，确保信封解析异常时 except 分支仍可安全引用。
    run_id: str | None = None
    step_id: str | None = None
    try:
        envelope = request.get("payload") or {}
        run_id = envelope.get("run_id") or request.get("run_id")
        step_id = envelope.get("step_id") or request.get("step_id")
        # 兼容两种信封形态：action_id 可能位于信封根或 payload 内。
        action_id = envelope.get("action_id") or request.get("action_id", "")
        plugin_id = envelope.get("plugin_id") or request.get("plugin_id", "")
        data = envelope.get("payload")
        if not isinstance(data, dict):
            # 扁平信封回退：参数直接铺在信封根层，过滤元字段后作为 payload。
            source = envelope if isinstance(envelope, dict) and envelope else request
            data = {k: v for k, v in source.items() if k not in _META_KEYS}
        if action_id not in _ACTION_IDS:
            return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)
        return execute_action(
            plugin_id, action_id, data,
            backend=backend, backend_name=backend_name,
            run_id=run_id, step_id=step_id,
        )
    except Exception as exc:  # noqa: BLE001 绝不向外抛出
        return _response(False, None, f"动作执行异常：{exc}", run_id=run_id, step_id=step_id)
