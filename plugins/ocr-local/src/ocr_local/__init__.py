"""本地 OCR 插件包。

可在两种模式下使用：
1. 独立模式：直接 ``import ocr_local`` 或运行 ``tools/ocr_cli.py``，
   现在就能对本地图片做 OCR / 找词点击。
2. fukuaRPA 插件模式：待 P3 宿主运行时落地后，由宿主按 manifest.json
   加载 ``src/ocr_local/action.py:run`` 执行。
"""

from .action import execute_action, run
from .backend import MockBackend, OcrBackend, RapidOcrBackend, create_backend
from .engine import ocr_image
from .matcher import click_point, find_target, match_text
from .models import OcrResult, OcrWord

__all__ = [
    "execute_action",
    "run",
    "OcrBackend",
    "RapidOcrBackend",
    "MockBackend",
    "create_backend",
    "ocr_image",
    "find_target",
    "click_point",
    "match_text",
    "OcrWord",
    "OcrResult",
]

__version__ = "1.1.0"
