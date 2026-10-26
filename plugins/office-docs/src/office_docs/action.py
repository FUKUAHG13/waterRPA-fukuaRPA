"""插件动作入口（fukuaRPA 插件契约实现）。

宿主进程（P3）加载插件后调用 ``run(request)``：按动作 id 前缀选择 Excel 或 PDF
后端，派发到具体文档动作，返回 PluginResponse 形状的字典 ``{"ok","result","error"}``。

权限模型（见 manifest.json）：
* ``files.user_selected`` —— 读/写用户显式指定的文档路径（来自步骤配置）。
* ``files.plugin_private_data`` —— 插件可写自己的临时/缓存数据（如拆分产物）。
* ``variables.write_declared`` —— 可把读到的值写入声明过的变量。
* 本插件**不声明任何网络权限**，文档处理完全离线。
"""

from __future__ import annotations

import threading
from typing import Any

from .engine import dispatch
from .excel_backend import _PDF_CACHE, _XL_CACHE, ExcelBackend, create_excel_backend
from .pdf_backend import PdfBackend, create_pdf_backend

# 支持的动作 id 白名单（与 manifest 的 actions 对齐）。
_ACTION_IDS = (
    "excel_read_cell", "excel_read_range", "excel_write_cell",
    "excel_write_range", "excel_find", "excel_sheets", "excel_row_count",
    "pdf_page_count", "pdf_extract_text", "pdf_search",
    "pdf_merge", "pdf_split",
)


# INVARIANT: 后端缓存受 _BACKEND_LOCK 保护，P3 并发宿主下安全。
# 单例后端本身仍非线程安全（OpenpyxlBackend.open() 会 close 旧簿），
# P3 若需真正并发执行同一插件的多个步骤，须改为每请求独立后端。
_BACKEND_LOCK = threading.Lock()


def _get_excel_backend(name: str = "openpyxl") -> ExcelBackend:
    with _BACKEND_LOCK:
        cached = _XL_CACHE.get(name)
        if cached is None:
            cached = create_excel_backend(name)
            _XL_CACHE[name] = cached
        return cached


def _get_pdf_backend(name: str = "pypdf") -> PdfBackend:
    with _BACKEND_LOCK:
        cached = _PDF_CACHE.get(name)
        if cached is None:
            cached = create_pdf_backend(name)
            _PDF_CACHE[name] = cached
        return cached


def _response(ok: bool, result: Any, error: str | None = None,
              run_id: str | None = None, step_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "result": result, "error": error}
    # 协议强制字段 run_id/step_id：仅当调用方（run）从信封取出并提供时才回显，
    # 以兼容不携带信封的逻辑核心 execute_action / CLI（修复 P2 响应缺字段）。
    if run_id is not None:
        out["run_id"] = run_id
    if step_id is not None:
        out["step_id"] = step_id
    return out


def _backend_for(action_id: str, backend_name: str | None):
    if action_id.startswith("excel_"):
        return _get_excel_backend(backend_name or "openpyxl")
    if action_id.startswith("pdf_"):
        return _get_pdf_backend(backend_name or "pypdf")
    raise ValueError(f"未知动作：{action_id}")


def execute_action(
    plugin_id: str,
    action_id: str,
    payload: dict[str, Any],
    *,
    backend: Any | None = None,
    backend_name: str | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    """执行一个办公文档动作，返回 PluginResponse 形状的字典。"""

    if action_id not in _ACTION_IDS:
        return _response(False, None, f"未知动作：{action_id}", run_id=run_id, step_id=step_id)

    own_backend = backend is None
    if own_backend:
        try:
            # Excel 与 PDF 共用 backend_name 参数位置；按前缀选择具体类型。
            backend = _backend_for(action_id, backend_name)
        except Exception as exc:  # noqa: BLE001
            return _response(False, None, f"无法创建文档后端：{exc}", run_id=run_id, step_id=step_id)

    try:
        data = dispatch(backend, action_id, payload or {})
        return _response(True, data, run_id=run_id, step_id=step_id)
    except Exception as exc:  # noqa: BLE001
        return _response(False, None, f"文档动作执行失败：{exc}", run_id=run_id, step_id=step_id)


def run(
    request: dict[str, Any],
    *,
    backend: Any | None = None,
    backend_name: str | None = None,
) -> dict[str, Any]:
    """宿主进程（P3）调用入口。入参为 PluginProtocol 的 request 信封。"""

    # 信封里的 run_id/step_id 需回显到响应（plugin_protocol 响应分支强制）。
    envelope = request.get("payload") or {}
    run_id = envelope.get("run_id") or request.get("run_id")
    step_id = envelope.get("step_id") or request.get("step_id")
    try:
        # 兼容两种信封形态：action_id 可能位于信封根或 payload 内。
        action_id = envelope.get("action_id") or request.get("action_id", "")
        plugin_id = envelope.get("plugin_id") or request.get("plugin_id", "")
        data = envelope.get("payload")
        if not isinstance(data, dict):
            # 扁平信封回退：部分宿主调用路径把动作参数直接放在信封根（而非嵌套在
            # payload.payload），并混入 run_id/step_id 等协议元数据键。去掉元数据键
            # 后作为动作参数，避免参数被静默丢弃（与 browser / screen-recorder 插件
            # 保持一致，修复 P2 潜在参数丢失）。
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
