"""办公文档动作编排（不依赖宿主，便于复用与测试）。

``dispatch`` 接收已构造好的后端（Excel 或 PDF）与动作参数，返回纯数据字典；
参数缺失/非法会抛 ``ValueError``，由 ``action.execute_action`` 转成失败响应。
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:  # 宿主环境提供脱敏工具；独立复用/测试上下文缺 fukua_rpa 时回退为原样返回。
    from fukua_rpa.privacy import redact_sensitive_paths
except Exception:  # pragma: no cover - 仅在非宿主上下文触发
    def redact_sensitive_paths(text: str) -> str:  # type: ignore[no-redef]
        return text

logger = logging.getLogger(__name__)


def dispatch(backend: Any, action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    # ---------------- Excel ----------------
    if action_id == "excel_read_cell":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        cell = _need(payload, "cell")
        _require_file_exists(path, backend)
        backend.open(path)
        return {"action": action_id, "value": backend.read_cell(sheet, cell)}

    if action_id == "excel_read_range":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        start = _need(payload, "start")
        end = _need(payload, "end")
        _require_file_exists(path, backend)
        backend.open(path)
        return {"action": action_id, "values": backend.read_range(sheet, start, end)}

    if action_id == "excel_write_cell":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        cell = _need(payload, "cell")
        value = payload.get("value")
        if value is None:
            raise ValueError("excel_write_cell 需要 value")
        _notify_create_if_missing(path, backend)
        backend.open(path)
        backend.write_cell(sheet, cell, value)
        backend.save()
        return {"action": action_id, "written": True}

    if action_id == "excel_write_range":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        start = _need(payload, "start")
        values = payload.get("values")
        if not isinstance(values, list):
            raise ValueError("excel_write_range 需要 values（二维数组）")
        for i, row in enumerate(values):
            if not isinstance(row, list):
                raise ValueError(f"excel_write_range values[{i}] 不是数组（需要二维数组）")
        _notify_create_if_missing(path, backend)
        backend.open(path)
        backend.write_range(sheet, start, values)
        backend.save()
        return {"action": action_id, "written": True}

    if action_id == "excel_find":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        value = _need(payload, "value")
        mode = payload.get("mode", "contains")
        if mode not in ("contains", "equals"):
            raise ValueError(f"无效的 mode={mode!r}")
        _require_file_exists(path, backend)
        backend.open(path)
        return {"action": action_id, "addresses": backend.find(sheet, str(value), mode)}

    if action_id == "excel_sheets":
        path = _need(payload, "path")
        _require_file_exists(path, backend)
        backend.open(path)
        return {"action": action_id, "sheets": backend.sheet_names()}

    if action_id == "excel_row_count":
        path = _need(payload, "path")
        sheet = payload.get("sheet", "")
        _require_file_exists(path, backend)
        backend.open(path)
        return {"action": action_id, "rows": backend.row_count(sheet)}

    # ---------------- PDF ----------------
    if action_id == "pdf_page_count":
        path = _need(payload, "path")
        return {"action": action_id, "pages": backend.page_count(path)}

    if action_id == "pdf_extract_text":
        path = _need(payload, "path")
        pages = payload.get("pages")
        return {"action": action_id, "text": backend.extract_text(path, pages)}

    if action_id == "pdf_search":
        path = _need(payload, "path")
        query = _need(payload, "query")
        mode = payload.get("mode", "contains")
        if mode not in ("contains", "regex"):
            raise ValueError(f"无效的 mode={mode!r}")
        return {"action": action_id, "matches": backend.search(path, query, mode)}

    if action_id == "pdf_merge":
        inputs = payload.get("inputs")
        output = _need(payload, "output")
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("pdf_merge 需要非空 inputs 数组")
        return {"action": action_id, "pages": backend.merge(inputs, output)}

    if action_id == "pdf_split":
        path = _need(payload, "path")
        ranges = _need(payload, "ranges")
        output_dir = _need(payload, "output_dir")
        return {"action": action_id, "outputs": backend.split(path, ranges, output_dir)}

    raise ValueError(f"未知动作：{action_id}")


def _require_file_exists(path: str, backend: Any) -> None:
    """读类动作前置检查：文件不存在时立即报错，避免静默返回空工作簿数据。

    Mock 后端（requires_real_file=False）使用虚拟路径，跳过磁盘检查。
    """
    if not getattr(backend, "requires_real_file", True):
        return
    if not os.path.exists(path):
        raise ValueError(f"文件不存在: {path}")


def _notify_create_if_missing(path: str, backend: Any) -> None:
    """写类动作前置提示：目标文件不存在时将被静默新建，记录日志以便排查。

    写动作允许新建文件（不报错），但记录一条 info 提示，避免用户误以为
    覆盖了已有文件。Mock 后端（requires_real_file=False）使用虚拟路径，跳过。
    """
    if not getattr(backend, "requires_real_file", True):
        return
    if not os.path.exists(path):
        logger.info("excel_write: 目标文件不存在，将新建: %s", redact_sensitive_paths(path))


def _need(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise ValueError(f"缺少参数：{key}")
    return payload[key]
