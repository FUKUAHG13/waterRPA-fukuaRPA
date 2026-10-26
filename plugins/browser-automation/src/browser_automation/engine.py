"""浏览器动作编排（不依赖宿主，便于复用与测试）。

``dispatch`` 接收已构造好的后端与动作参数，返回纯数据字典；
未知动作或参数缺失会抛 ``ValueError``，由 ``action.execute_action`` 转成
失败响应，保证宿主/运行不会因单个动作崩溃。
"""

from __future__ import annotations

from typing import Any

_VALID_BY = ("text", "css", "xpath")


def _locator(payload: dict[str, Any], require_value: bool) -> dict[str, Any]:
    by = payload.get("by", "text")
    if by not in _VALID_BY:
        raise ValueError(f"无效的定位方式 by={by!r}，应为 text/css/xpath")
    value = payload.get("value")
    if require_value and not value:
        raise ValueError("缺少定位值 value（by=text 时传文本，by=css/xpath 时传选择器）")
    try:
        index = int(payload.get("index", 0))
    except (TypeError, ValueError):
        raise ValueError("index 必须是整数") from None
    if index < 0:
        # 负索引会被 Python 解释为「末位元素」，属越界输入，应明确拒绝而非静默误点。
        raise ValueError("index 必须为非负整数（0 基前向计数）")
    return {"by": by, "value": value, "index": index}


def dispatch(backend: Any, action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action_id == "browser_navigate":
        url = payload.get("url")
        if not url:
            raise ValueError("browser_navigate 需要 url")
        wait_until = payload.get("wait_until", "load")
        if wait_until not in ("load", "domcontentloaded", "networkidle"):
            raise ValueError(f"无效的 wait_until={wait_until!r}")
        headless = bool(payload.get("headless", True))
        timeout_ms = int(payload.get("timeout_ms", 30000))
        backend.ensure_browser(headless)
        got = backend.navigate(url, wait_until, timeout_ms)
        return {"action": "browser_navigate", "url": got, "found": True}

    if action_id == "browser_click":
        loc = _locator(payload, require_value=True)
        el = backend.click(loc["by"], loc["value"], loc["index"])
        return {"action": "browser_click", "found": True, "element": el.to_dict()}

    if action_id == "browser_fill":
        loc = _locator(payload, require_value=True)
        text = payload.get("text")
        if text is None:
            raise ValueError("browser_fill 需要 text")
        el = backend.fill(loc["by"], loc["value"], str(text), loc["index"])
        return {"action": "browser_fill", "found": True, "element": el.to_dict()}

    if action_id == "browser_extract_text":
        by = payload.get("by")
        value = payload.get("value")
        if by and by not in _VALID_BY:
            raise ValueError(f"无效的定位方式 by={by!r}")
        text = backend.get_text(by, value)
        return {"action": "browser_extract_text", "found": text is not None, "text": text or ""}

    if action_id == "browser_wait_for":
        loc = _locator(payload, require_value=True)
        timeout_ms = int(payload.get("timeout_ms", 5000))
        ok = backend.wait_for(loc["by"], loc["value"], timeout_ms)
        return {"action": "browser_wait_for", "found": ok}

    if action_id == "browser_get_attribute":
        loc = _locator(payload, require_value=True)
        attr = payload.get("attr")
        if not attr:
            raise ValueError("browser_get_attribute 需要 attr")
        val = backend.get_attribute(loc["by"], loc["value"], attr, loc["index"])
        return {"action": "browser_get_attribute", "found": True, "value": val}

    if action_id == "browser_screenshot":
        path = payload.get("path")
        if not path:
            raise ValueError("browser_screenshot 需要 path")
        by = payload.get("by")
        value = payload.get("value")
        if by and by not in _VALID_BY:
            raise ValueError(f"无效的定位方式 by={by!r}")
        saved = backend.screenshot(path, by, value)
        return {"action": "browser_screenshot", "found": True, "path": saved}

    raise ValueError(f"未知动作：{action_id}")
