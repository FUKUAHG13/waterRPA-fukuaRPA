"""插件动作入口（fukuaRPA 插件契约实现）。

宿主进程（P3）加载插件后调用 ``run(request)``：request 是 PluginProtocol 的
request 信封，其 ``payload`` 内包含 ``action_id`` 与本步骤的 ``plugin_config``
（即用户填的参数）。本模块派发到具体浏览器动作，返回 PluginResponse 形状的
字典 ``{"ok", "result", "error"}``。

权限模型（见 manifest.json；顶层 permissions 恒等于各动作 permissions 的并集，
由 plugins/tools/common.py 的 --validate 门禁强制）：
* ``process.launch_confirmed`` —— 显式声明会启动浏览器子进程（用户已确认）。
* ``screen.capture_region`` —— browser_screenshot 元素截图的回退能力。
* ``files.user_selected`` —— browser_screenshot 把图片写入用户指定的路径。
* ``variables.write_declared`` —— 可把提取到的文本/属性写入声明过的变量。
* 本插件**不申请** ``window.query`` / ``window.control``：全部 DOM 操作经
  Playwright 完成，代码中没有任何窗口定位或聚焦调用。二者曾作为 P3 预留写在
  顶层权限里，会让用户在安装确认页高估本插件能力，已移除；将来真正迁移到宿主
  窗口能力时，先给具体动作加权限，再由门禁驱动顶层同步。
* 本插件**不声明任何网络权限**：联网行为来自用户主动启动的浏览器子进程，
  不是 fukuaRPA 主程序对外通信（见 README 信任边界说明）。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from .backend import _BACKEND_CACHE, BrowserBackend, create_backend
from .engine import dispatch

# 支持的动作 id 白名单（与 manifest 的 actions 对齐）。
_ACTION_IDS = (
    "browser_navigate",
    "browser_click",
    "browser_fill",
    "browser_extract_text",
    "browser_wait_for",
    "browser_get_attribute",
    "browser_screenshot",
)


# INVARIANT: 后端缓存受 _BACKEND_LOCK 保护，P3 并发宿主下安全。
# 单例后端本身仍非线程安全（PlaywrightBackend 的浏览器实例在 ensure_browser() 时可能关闭旧连接），
# P3 若需真正并发执行同一插件的多个步骤，须改为每请求独立后端。
_BACKEND_LOCK = threading.Lock()


def _get_backend(name: str = "playwright") -> BrowserBackend:
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
    # 以兼容不携带信封的逻辑核心 execute_action / CLI（对齐 office-docs 行为）。
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
    backend: BrowserBackend | None = None,
    backend_name: str = "playwright",
    auto_close: bool = False,
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    """执行一个浏览器动作，返回 PluginResponse 形状的字典。"""

    if action_id not in _ACTION_IDS:
        return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)

    own_backend = backend is None
    if own_backend:
        try:
            backend = _get_backend(backend_name)
        except Exception as exc:  # noqa: BLE001
            return _response(False, None, f"无法创建浏览器后端：{exc}", run_id=run_id, step_id=step_id)

    try:
        data = dispatch(backend, action_id, payload or {})
        return _response(True, data, run_id=run_id, step_id=step_id)
    except Exception as exc:  # noqa: BLE001
        return _response(False, None, f"浏览器动作执行失败：{exc}", run_id=run_id, step_id=step_id)
    finally:
        # FR-20260813-D11-001: 只要调用方显式要求 auto_close，就必须关闭 backend，
        # 不论是否由本函数创建（own_backend）。此前 guard 要求 own_backend，导致 CLI
        # 自建并传入的 backend（未入 _BACKEND_CACHE）在 auto_close=True 时漏关、逃过
        # atexit，造成浏览器子进程泄漏。run() 宿主路径不传 auto_close，不受影响。
        if auto_close and backend is not None:
            with contextlib.suppress(Exception):
                backend.close()


def run(
    request: dict[str, Any],
    *,
    backend: BrowserBackend | None = None,
    backend_name: str = "playwright",
) -> dict[str, Any]:
    """宿主进程（P3）调用入口。入参为 PluginProtocol 的 request 信封。

    ``backend`` / ``backend_name`` 由宿主注入；默认走 playwright 后端。宿主若
    要复用已启动的浏览器实例，应把实例作为 ``backend`` 传入。
    """

    # 信封里的 run_id/step_id 需回显到响应（plugin_protocol 响应分支强制），
    # 与 office-docs 保持一致：优先取信封根（payload），回退取请求根。
    run_id = None
    step_id = None
    try:
        envelope = request.get("payload") or {}
        run_id = envelope.get("run_id") or request.get("run_id")
        step_id = envelope.get("step_id") or request.get("step_id")
        # action_id / plugin_id 位于契约标准信封的 payload 内；同时兼容放在请求根的扁平形态。
        action_id = envelope.get("action_id") or request.get("action_id", "")
        plugin_id = envelope.get("plugin_id") or request.get("plugin_id", "")
        # 标准信封：参数嵌套在 payload.payload；扁平形态（无嵌套 payload）则参数直接位于
        # envelope/请求根，去掉协议元数据键后作为 plugin_config，避免参数被静默丢弃。
        data = envelope.get("payload")
        if not isinstance(data, dict):
            _META_KEYS = {
                "run_id", "step_id", "plugin_id", "action_id", "deadline_ns",
                "protocol", "version", "kind",
            }
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
