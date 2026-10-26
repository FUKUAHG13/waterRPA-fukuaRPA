"""浏览器自动化插件 —— 数据模型。

定义动作返回的元素与结果结构。坐标 ``bounding_box`` 使用浏览器 CSS 像素
（与屏幕像素在 DPI=100% 时一致；宿主点击时会由 fukuaRPA 的窗口映射换算到
实际屏幕坐标，无需本插件关心 DPI）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrowserElement:
    """页面中一个被定位到的元素。"""

    index: int
    text: str
    selector: str
    tag: str
    attributes: dict[str, str] = field(default_factory=dict)
    # CSS 像素包围盒 {x, y, width, height}；截图/点击定位用。
    bounding_box: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "selector": self.selector,
            "tag": self.tag,
            "attributes": dict(self.attributes),
            "bounding_box": dict(self.bounding_box) if self.bounding_box else None,
        }


@dataclass
class BrowserResult:
    """一次浏览器动作的结果。"""

    action: str
    url: str | None = None
    elements: list[BrowserElement] = field(default_factory=list)
    text: str = ""
    value: str | None = None
    path: str | None = None
    found: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "url": self.url,
            "elements": [e.to_dict() for e in self.elements],
            "text": self.text,
            "value": self.value,
            "path": self.path,
            "found": self.found,
        }
