#!/usr/bin/env python3
"""构建「办公文档」插件：生成 manifest.json（含逐文件 SHA-256）并打包成 .fkrplugin。

用法
----
    python tools/build_plugin.py            # 生成 manifest.json + office-docs.fkrplugin
    python tools/build_plugin.py --no-zip   # 只生成 manifest.json，不打包
    python tools/build_plugin.py --validate # 用原项目契约严格校验（需能 import 到 fukua_rpa.plugin_manifest）
    python tools/build_plugin.py --check    # 校验磁盘 manifest.json 与源码生成逻辑一致（防漂移）

约定
----
* 本脚本只读写 plugins/office-docs/ 自身目录，绝不修改原 fukuaRPA 项目。
* 打包内容 = manifest.json + 运行时文件（src/、README.md、requirements.txt、LICENSE）。
  tools/ 与 tests/ 属开发期产物，不进可分发包。
* 共享构建逻辑见 plugins/tools/common.py；本文件保留插件特有的运行时文件清单、清单元数据，
  以及 action ids 与代码 _ACTION_IDS 的一致性校验。
"""

from __future__ import annotations

import ast
import os
import sys

# 复用 plugins/tools/common.py 的共享构建逻辑。
# build_plugin.py 位于 plugins/<id>/tools/，上三级为 plugins/，共享工具在其 tools/ 子目录。
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
))

from common import check_manifest_in_sync as _common_check
from common import main as _common_main  # noqa: E402
from common import write_manifest as _common_write

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运行时文件（相对插件根目录）。这些既进包、也写入 manifest.files 做完整性校验。
RUNTIME_FILES = [
    "README.md",
    "requirements.txt",
    "LICENSE",
    "src/office_docs/__init__.py",
    "src/office_docs/action.py",
    "src/office_docs/engine.py",
    "src/office_docs/excel_backend.py",
    "src/office_docs/pdf_backend.py",
]

MANIFEST_META = {
    "format": "fukuaRPA.plugin",
    "manifest_version": 1,
    "id": "office-docs",
    "name": "办公文档（Excel / PDF）",
    "version": "1.1.0",
    "author": "fukuaRPA 社区插件",
    "plugin_type": "python_action",
    "_note_p2": "P2 阶段：当前为 Python 实现（python_action），非 WASM 沙箱",
    "api_version": 1,
    "min_app_version": "1.0.0",
    "entrypoint": "src/office_docs/action.py:run",
    # 离线文档处理：只声明文件读写（用户选定）/ 插件私有数据 / 写变量三类权限，
    # 不申请任何网络权限。
    "permissions": [
        "files.user_selected",
        "files.plugin_private_data",
        "variables.write_declared",
    ],
    # manifest 的 dependencies 仅用于「插件间依赖」（需为插件 id）；pip 运行时
    # 依赖写在 requirements.txt，不在此声明。
    "dependencies": [],
    "templates": [],
    "actions": [
        # ---- Excel ----
        {
            "id": "excel_read_cell",
            "name": "Excel 读取单元格",
            "description": "读取指定工作表某单元格的值（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名（空=活动表）"},
                    "cell": {"type": "string", "title": "单元格，如 A1"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"value": {"type": ["string", "number", "boolean", "null"]}},
            },
        },
        {
            "id": "excel_read_range",
            "name": "Excel 读取区域",
            "description": "读取矩形区域，返回二维数组（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名"},
                    "start": {"type": "string", "title": "起点，如 A1"},
                    "end": {"type": "string", "title": "终点，如 C3"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"values": {"type": "array"}},
            },
        },
        {
            "id": "excel_write_cell",
            "name": "Excel 写入单元格",
            "description": "写入指定单元格并保存（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名"},
                    "cell": {"type": "string", "title": "单元格，如 A1"},
                    "value": {"type": ["string", "number", "boolean"], "title": "写入值"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"written": {"type": "boolean"}},
            },
        },
        {
            "id": "excel_write_range",
            "name": "Excel 写入区域",
            "description": "从起点批量写入二维数据并保存（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名"},
                    "start": {"type": "string", "title": "起点，如 A1"},
                    "values": {"type": "array", "title": "二维数组"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"written": {"type": "boolean"}},
            },
        },
        {
            "id": "excel_find",
            "name": "Excel 查找值",
            "description": "在工作表中查找值，返回所有命中单元格地址（.xlsx）。",
            "permissions": ["files.user_selected", "variables.write_declared"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名"},
                    "value": {"type": "string", "title": "要查找的值"},
                    "mode": {"type": "string", "enum": ["contains", "equals"], "default": "contains"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"addresses": {"type": "array"}},
            },
        },
        {
            "id": "excel_sheets",
            "name": "Excel 列出工作表",
            "description": "返回工作簿中所有工作表名称（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "title": "xlsx 路径"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"sheets": {"type": "array"}},
            },
        },
        {
            "id": "excel_row_count",
            "name": "Excel 行数",
            "description": "返回某工作表的最大行数（.xlsx）。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "xlsx 路径"},
                    "sheet": {"type": "string", "title": "工作表名"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"rows": {"type": "integer"}},
            },
        },
        # ---- PDF ----
        {
            "id": "pdf_page_count",
            "name": "PDF 页数",
            "description": "返回 PDF 文件页数。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "title": "pdf 路径"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"pages": {"type": "integer"}},
            },
        },
        {
            "id": "pdf_extract_text",
            "name": "PDF 提取文本",
            "description": "提取全部或指定页（如 1-3,5）的文本内容。",
            "permissions": ["files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "pdf 路径"},
                    "pages": {"type": "string", "title": "页码范围，空=全部"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
        {
            "id": "pdf_search",
            "name": "PDF 搜索文本",
            "description": "在 PDF 中搜索关键词，返回命中页码与上下文片段。",
            "permissions": ["files.user_selected", "variables.write_declared"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "pdf 路径"},
                    "query": {"type": "string", "title": "查询词或正则"},
                    "mode": {"type": "string", "enum": ["contains", "regex"], "default": "contains"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"matches": {"type": "array"}},
            },
        },
        {
            "id": "pdf_merge",
            "name": "PDF 合并",
            "description": "将多个 PDF 合并为一个，返回输出页数。",
            "permissions": ["files.user_selected", "files.plugin_private_data"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "inputs": {"type": "array", "title": "输入 pdf 路径列表"},
                    "output": {"type": "string", "title": "输出路径"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"pages": {"type": "integer"}},
            },
        },
        {
            "id": "pdf_split",
            "name": "PDF 拆分",
            "description": "按页范围（如 1-2,3）拆分成多个 PDF，返回生成路径列表。",
            "permissions": ["files.user_selected", "files.plugin_private_data"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "pdf 路径"},
                    "ranges": {"type": "string", "title": "页范围，如 1-2,3"},
                    "output_dir": {"type": "string", "title": "输出目录"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"outputs": {"type": "array"}},
            },
        },
    ],
}



def _check_actions_match_code() -> bool:
    """校验 MANIFEST_META 中声明的 action ids 与代码中 _ACTION_IDS 一致。

    使用 AST 解析避免相对导入问题（action.py 含 from .engine import ...）。
    """
    action_path = os.path.join(PLUGIN_ROOT, "src", "office_docs", "action.py")
    try:
        with open(action_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as exc:
        print(f"  无法解析 action.py: {exc}")
        return False
    code_ids: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ACTION_IDS" and isinstance(node.value, (ast.Tuple, ast.List)):
                        code_ids = {
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        }
    if not code_ids:
        print("  未在 action.py 中找到 _ACTION_IDS 定义")
        return False
    meta_ids = {a["id"] for a in MANIFEST_META.get("actions", [])}
    if code_ids != meta_ids:
        print(f"  action id 漂移: 代码={sorted(code_ids)} 清单={sorted(meta_ids)}")
        return False
    return True


def check_manifest_in_sync(verbose=True):
    """Plugin-specific wrapper for test compatibility."""
    return _common_check(PLUGIN_ROOT, MANIFEST_META, RUNTIME_FILES, verbose=verbose)


def write_manifest_local():
    """Plugin-specific wrapper."""
    _common_write(PLUGIN_ROOT, MANIFEST_META, RUNTIME_FILES)

if __name__ == "__main__":
    _common_main(
        PLUGIN_ROOT,
        MANIFEST_META,
        RUNTIME_FILES,
        "构建办公文档插件",
        extra_check=_check_actions_match_code,
    )
