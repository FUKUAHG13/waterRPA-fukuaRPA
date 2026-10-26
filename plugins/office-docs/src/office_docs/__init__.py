"""办公文档插件包（Excel + PDF）。

两种用法：
1. 独立模式：``import office_docs`` 或运行 ``tools/office_cli.py``，直接对本地
   文档做读写/查找/合并拆分（需自装 openpyxl/pypdf）。
2. fukuaRPA 插件模式：待 P3 宿主运行时落地后，由宿主按 manifest.json 加载
   ``src/office_docs/action.py:run`` 执行。
"""

from .action import execute_action, run
from .engine import dispatch
from .excel_backend import (
    ExcelBackend,
    MockExcelBackend,
    OpenpyxlBackend,
    create_excel_backend,
)
from .pdf_backend import (
    MockPdfBackend,
    PdfBackend,
    PypdfBackend,
    create_pdf_backend,
)

__all__ = [
    "execute_action",
    "run",
    "ExcelBackend",
    "OpenpyxlBackend",
    "MockExcelBackend",
    "create_excel_backend",
    "PdfBackend",
    "PypdfBackend",
    "MockPdfBackend",
    "create_pdf_backend",
    "dispatch",
]

__version__ = "1.1.0"
