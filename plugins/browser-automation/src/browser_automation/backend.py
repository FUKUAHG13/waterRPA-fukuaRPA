"""浏览器后端抽象与两种实现。

* ``PlaywrightBackend``：真实驱动 Chromium/WebKit/Firefox，做 DOM 级自动化。
  依赖 ``playwright``（需用户 ``pip install playwright`` 并 ``playwright install``）。
  所有重导入均为**惰性**，因此本插件在没装 Playwright 时也能被 import、被
  fukuaRPA 列出/校验，只是运行该动作时才会提示安装。
* ``MockBrowserBackend``：内置一组固定页面数据，用于单元测试与无浏览器环境的
  逻辑冒烟，完全不依赖任何第三方库。

信任边界说明：浏览器自动化必然要启动一个浏览器子进程并由它访问网址——那是
用户**显式选装并主动使用**的能力，属于用户自己控制的子进程，不意味着 fukuaRPA
主程序"联网"。fukuaRPA 核心仍保持离线；本插件通过 ``process.launch_confirmed``
权限明确声明"会启动确认过的进程"。
"""

from __future__ import annotations

import contextlib
import os
from abc import ABC, abstractmethod
from dataclasses import replace

from .models import BrowserElement


def _is_timeout_error(exc: Exception) -> bool:
    """判断 Playwright 异常是否为「等待超时」而非真实错误。

    Playwright 旧版本直接抛内置 ``TimeoutError``；新版本抛出自有的
    ``playwright._impl._api_types.Error``（``.name == "TimeoutError"``）。
    超时属于预期的"未找到"，应返回 ``False``；其它错误需向上抛出。
    """
    if isinstance(exc, TimeoutError):
        return True
    name = getattr(exc, "name", None)
    return name in ("TimeoutError", "Timeout")


class BrowserBackend(ABC):
    """浏览器动作的底层能力。所有方法在实现层自行保证异常可被上层捕获。"""

    @abstractmethod
    def ensure_browser(self, headless: bool) -> None:
        """确保已启动浏览器驱动（首次导航前调用）。"""

    @abstractmethod
    def navigate(self, url: str, wait_until: str, timeout_ms: int) -> str:
        """导航到 url，返回最终落地地址；wait_until ∈ {load,domcontentloaded,networkidle}。"""

    @abstractmethod
    def find_elements(self, by: str, value: str, max_elements: int = 200) -> list[BrowserElement]:
        """按定位方式返回元素列表。by ∈ {text,css,xpath}。"""

    @abstractmethod
    def click(self, by: str, value: str, index: int) -> BrowserElement:
        """点击第 index 个匹配元素，返回该元素。"""

    @abstractmethod
    def fill(self, by: str, value: str, text: str, index: int) -> BrowserElement:
        """向第 index 个匹配输入框填入 text，返回该元素。"""

    @abstractmethod
    def get_text(self, by: str | None, value: str | None) -> str | None:
        """取文本：by/value 为空取整页，否则取匹配元素文本；元素不存在时返回 None。"""

    @abstractmethod
    def get_attribute(self, by: str, value: str, attr: str, index: int) -> str:
        """取第 index 个匹配元素的属性值。"""

    @abstractmethod
    def wait_for(self, by: str, value: str, timeout_ms: int) -> bool:
        """等待元素出现，超时返回 False。"""

    @abstractmethod
    def screenshot(self, path: str, by: str | None, value: str | None) -> str:
        """截图整页或某元素到 path，返回实际保存路径。"""

    @abstractmethod
    def close(self) -> None:
        """释放浏览器资源。"""


# --------------------------------------------------------------------------
# 真实后端：Playwright（惰性导入）
# --------------------------------------------------------------------------

class PlaywrightBackend(BrowserBackend):
    def __init__(self, browser_name: str = "chromium") -> None:
        self._browser_name = browser_name
        self._pw = None
        self._browser = None
        self._page = None

    def ensure_browser(self, headless: bool) -> None:
        if self._browser is not None:
            if self._browser.is_connected():
                return
            # 浏览器进程已崩溃/断开：缓存里持的是失效实例，直接复用会持续报错。
            # 清理残留句柄后 fall through 重建（修复 R-browser-lifecycle）。
            self._cleanup_broken_browser()
        from playwright.sync_api import sync_playwright  # 惰性

        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self._browser_name)
        self._browser = launcher.launch(headless=headless)
        self._page = self._browser.new_page()

    def _cleanup_broken_browser(self) -> None:
        """安全释放已崩溃/断开的浏览器句柄，忽略清理过程中的任何异常。"""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
        self._browser = None
        self._page = None
        self._pw = None

    def _page_required(self):
        if self._page is None:
            raise RuntimeError("尚未导航：请先执行 browser_navigate")
        return self._page

    def _loc(self, by: str, value: str):
        if by == "text":
            return f"text={value}"
        if by == "css":
            return value
        if by == "xpath":
            if not value.startswith(("//", "..")):
                return f"xpath={value}"
            return value
        raise ValueError(f"不支持的定位方式：{by}")

    def navigate(self, url: str, wait_until: str, timeout_ms: int) -> str:
        # headless 由 dispatch 统一通过 ensure_browser(headless) 应用用户设置；
        # 此处不再重复传入硬编码 headless=True（该参数永不生效，属冗余调用）。
        page = self._page_required()
        # timeout_ms=0 在 Playwright 中表示「无限等待」，会导致导航永久挂起。
        # 设最小正下限 1 秒，防止无限等待（修复 R-timeout）。
        timeout_ms = max(timeout_ms, 1000)
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        return page.url

    def find_elements(self, by: str, value: str, max_elements: int = 200) -> list[BrowserElement]:
        max_elements = max(1, max_elements)
        page = self._page_required()
        selector = self._loc(by, value)
        nodes = page.query_selector_all(selector)
        out: list[BrowserElement] = []
        # 限制单次返回的元素数量，避免对超大 DOM 无上限遍历（默认 200）。
        for i, node in enumerate(nodes[:max_elements]):
            bb = node.bounding_box()
            out.append(
                BrowserElement(
                    index=i,
                    text=(node.inner_text() or "").strip(),
                    selector=selector,
                    tag=(node.evaluate("el => el.tagName.toLowerCase()") or "unknown"),
                    attributes=node.evaluate("el => Object.fromEntries([...el.attributes].map(a => [a.name, a.value]))") or {},
                    bounding_box=(
                        {"x": bb["x"], "y": bb["y"], "width": bb["width"], "height": bb["height"]}
                        if bb else None
                    ),
                )
            )
        return out

    def click(self, by: str, value: str, index: int) -> BrowserElement:
        page = self._page_required()
        node = page.query_selector_all(self._loc(by, value))[index]
        bb = node.bounding_box()
        node.click()
        return BrowserElement(
            index=index,
            text=(node.inner_text() or "").strip(),
            selector=self._loc(by, value),
            tag=node.evaluate("el => el.tagName.toLowerCase()") or "unknown",
            attributes=node.evaluate("el => Object.fromEntries([...el.attributes].map(a => [a.name, a.value]))") or {},
            bounding_box=(
                {"x": bb["x"], "y": bb["y"], "width": bb["width"], "height": bb["height"]}
                if bb else None
            ),
        )

    def fill(self, by: str, value: str, text: str, index: int) -> BrowserElement:
        page = self._page_required()
        node = page.query_selector_all(self._loc(by, value))[index]
        node.fill(text)
        return BrowserElement(
            index=index,
            text=text,
            selector=self._loc(by, value),
            tag=node.evaluate("el => el.tagName.toLowerCase()") or "input",
            attributes=node.evaluate("el => Object.fromEntries([...el.attributes].map(a => [a.name, a.value]))") or {},
        )

    def get_text(self, by: str | None, value: str | None) -> str | None:
        page = self._page_required()
        if by and value:
            node = page.query_selector(self._loc(by, value))
            return node.inner_text() if node else None
        return page.inner_text()

    def get_attribute(self, by: str, value: str, attr: str, index: int) -> str:
        page = self._page_required()
        node = page.query_selector_all(self._loc(by, value))[index]
        return node.get_attribute(attr) or ""

    def wait_for(self, by: str, value: str, timeout_ms: int) -> bool:
        page = self._page_required()
        # timeout_ms=0 在 Playwright 中表示「无限等待」，会导致等待永久挂起。
        # 设最小正下限 1 秒，防止无限等待（修复 R-timeout）。
        timeout_ms = max(timeout_ms, 1000)
        try:
            page.wait_for_selector(self._loc(by, value), timeout=timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001
            # 区分「超时未出现」（预期，返回 False）与「其它异常」（如选择器语法错误、
            # 页面已关闭、上下文失效等真实错误）。后者不得伪装成"未找到"（修复 P3 吞异常
            # 缺陷），向上抛出由上层 action 转成带错误信息的失败响应。
            if _is_timeout_error(exc):
                return False
            raise

    def screenshot(self, path: str, by: str | None, value: str | None) -> str:
        page = self._page_required()
        if by and value:
            node = page.query_selector(self._loc(by, value))
            if node is None:
                # 选择器无匹配时 query_selector 返回 None；直接 node.screenshot() 会抛
                # AttributeError。改为抛出带定位信息的 ValueError，由上层 execute_action
                # 转成 ok:False 的失败响应（与 click/fill 越界抛错的约定一致）。
                raise ValueError(f"未找到元素: {by}={value}")
            node.screenshot(path=path)
        else:
            page.screenshot(path=path)
        return os.path.abspath(path)

    def close(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
        self._browser = None
        self._page = None
        self._pw = None


# --------------------------------------------------------------------------
# 测试后端：Mock
# --------------------------------------------------------------------------

class MockBrowserBackend(BrowserBackend):
    """一组固定的内置页面，便于无浏览器环境下验证插件逻辑。"""

    _PAGE_TEXT = "欢迎使用 fukuaRPA 登录 用户名 密码 提交"
    _ELEMENTS = [
        BrowserElement(
            0, "登录", "text=登录", "button",
            {"id": "btn-login", "type": "submit"},
            {"x": 100.0, "y": 220.0, "width": 80.0, "height": 32.0},
        ),
        BrowserElement(
            1, "用户名", "input[name=username]", "input",
            {"name": "username", "placeholder": "请输入用户名"},
            {"x": 100.0, "y": 100.0, "width": 200.0, "height": 28.0},
        ),
        BrowserElement(
            2, "密码", "input[name=password]", "input",
            {"name": "password", "type": "password"},
            {"x": 100.0, "y": 150.0, "width": 200.0, "height": 28.0},
        ),
    ]

    def __init__(self) -> None:
        self._url: str | None = None
        self._values: dict[str, str] = {}

    def ensure_browser(self, headless: bool) -> None:
        self._url = self._url or "about:blank"

    def navigate(self, url: str, wait_until: str, timeout_ms: int) -> str:
        self._url = url
        return url

    def find_elements(self, by: str, value: str, max_elements: int = 200) -> list[BrowserElement]:
        out: list[BrowserElement] = []
        for el in self._ELEMENTS:
            if by == "text" and value in el.text or by in ("css", "xpath") and value in el.selector:
                out.append(el)
        return out[:max_elements]

    def click(self, by: str, value: str, index: int) -> BrowserElement:
        els = self.find_elements(by, value)
        if index >= len(els):
            raise IndexError(f"元素索引越界：{index} >= {len(els)}")
        return els[index]

    def fill(self, by: str, value: str, text: str, index: int) -> BrowserElement:
        els = self.find_elements(by, value)
        if index >= len(els):
            raise IndexError(f"元素索引越界：{index} >= {len(els)}")
        el = els[index]
        self._values[el.selector] = text
        # 与 Playwright 后端保持一致：返回元素的 text 反映填入的值。
        return replace(el, text=text)

    def get_text(self, by: str | None, value: str | None) -> str | None:
        if by and value:
            els = self.find_elements(by, value)
            if not els:
                return None
            return " ".join(e.text for e in els)
        return self._PAGE_TEXT

    def get_attribute(self, by: str, value: str, attr: str, index: int) -> str:
        els = self.find_elements(by, value)
        if index >= len(els):
            raise IndexError(f"元素索引越界：{index} >= {len(els)}")
        return els[index].attributes.get(attr, "")

    def wait_for(self, by: str, value: str, timeout_ms: int) -> bool:
        return len(self.find_elements(by, value)) > 0

    def screenshot(self, path: str, by: str | None, value: str | None) -> str:
        # Mock 写一个小占位文件，证明路径回传链路可用。
        with open(path, "wb") as handle:
            handle.write(b"mock-screenshot-placeholder")
        return os.path.abspath(path)

    def close(self) -> None:
        self._url = None


def create_backend(name: str = "playwright") -> BrowserBackend:
    if name == "mock":
        return MockBrowserBackend()
    if name in ("playwright", "pw"):
        return PlaywrightBackend()
    raise ValueError(f"未知浏览器后端：{name!r}（可选：playwright / mock）")


# --------------------------------------------------------------------------
# 进程级单例缓存与生命周期管理
# --------------------------------------------------------------------------

# 进程级单例缓存：Playwright 启动浏览器较贵，避免每个动作重开。
_BACKEND_CACHE: dict[str, BrowserBackend] = {}


def get_backend(name: str = "playwright") -> BrowserBackend:
    """按名字取浏览器后端，进程内复用同一实例（单例）。"""
    cached = _BACKEND_CACHE.get(name)
    if cached is None:
        cached = create_backend(name)
        _BACKEND_CACHE[name] = cached
    return cached


def close_all() -> None:
    """释放所有缓存的浏览器后端并清空缓存。

    生命周期：本模块持有进程级单例缓存；宿主（P3）退出时通过 ``atexit`` 自动调用
    本函数，关闭浏览器子进程/驱动句柄以避免资源泄漏。幂等——重复调用安全。
    """
    for backend in list(_BACKEND_CACHE.values()):
        with contextlib.suppress(Exception):
            backend.close()
    _BACKEND_CACHE.clear()


try:
    import atexit
    atexit.register(close_all)
except Exception:  # noqa: BLE001 极少数无 atexit 的环境（如受限嵌入式解释器）
    pass
