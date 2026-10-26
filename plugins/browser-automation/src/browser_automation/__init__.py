"""浏览器自动化插件包。

两种用法：
1. 独立模式：``import browser_automation`` 或运行 ``tools/browser_cli.py``，
   直接对本地/远程网页做 DOM 级自动化（需自装 playwright）。
2. fukuaRPA 插件模式：待 P3 宿主运行时落地后，由宿主按 manifest.json 加载
   ``src/browser_automation/action.py:run`` 执行。
"""

from .action import execute_action, run
from .backend import (
    BrowserBackend,
    MockBrowserBackend,
    PlaywrightBackend,
    create_backend,
)
from .engine import dispatch
from .models import BrowserElement, BrowserResult

__all__ = [
    "execute_action",
    "run",
    "BrowserBackend",
    "PlaywrightBackend",
    "MockBrowserBackend",
    "create_backend",
    "dispatch",
    "BrowserElement",
    "BrowserResult",
]

__version__ = "1.1.0"
