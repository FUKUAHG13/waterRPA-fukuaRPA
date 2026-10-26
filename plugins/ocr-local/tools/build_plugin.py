#!/usr/bin/env python3
"""构建「本地 OCR」插件：生成 manifest.json（含逐文件 SHA-256）并打包成 .fkrplugin。

用法
----
    python tools/build_plugin.py            # 生成 manifest.json + ocr-local.fkrplugin
    python tools/build_plugin.py --no-zip   # 只生成 manifest.json，不打包
    python tools/build_plugin.py --validate # 用原项目契约严格校验（需能 import 到 fukua_rpa.plugin_manifest）
    python tools/build_plugin.py --check    # 校验磁盘 manifest.json 与源码生成逻辑一致（防漂移）

约定
----
* 本脚本只读写 plugins/ocr-local/ 自身目录，绝不修改原 fukuaRPA 项目。
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
    "src/ocr_local/__init__.py",
    "src/ocr_local/action.py",
    "src/ocr_local/backend.py",
    "src/ocr_local/engine.py",
    "src/ocr_local/io_image.py",
    "src/ocr_local/matcher.py",
    "src/ocr_local/models.py",
    "src/ocr_local/input_click.py",
]

MANIFEST_META = {
    "format": "fukuaRPA.plugin",
    "manifest_version": 1,
    "id": "ocr-local",
    "name": "本地 OCR（离线文字识别）",
    "version": "1.1.0",
    "author": "fukuaRPA 社区插件",
    "plugin_type": "python_action",
    "_note_p2": "P2 阶段：当前为 Python 实现（python_action），非 WASM 沙箱",
    "api_version": 1,
    "min_app_version": "1.0.0",
    "entrypoint": "src/ocr_local/action.py:run",
    # 离线插件：只声明屏幕截取 / 鼠标点击 / 写变量三类权限，不申请任何网络权限。
    "permissions": [
        "screen.capture_region",
        "input.mouse",
        "variables.write_declared",
    ],
    "dependencies": [],
    "templates": [],
    "actions": [
        {
            "id": "ocr_find_click",
            "name": "OCR 识别文字并点击",
            "description": (
                "对指定区域做离线文字识别，找到目标文本后返回其区域局部坐标"
                "（由宿主叠加区域原点得到屏幕坐标），由宿主通过 input.mouse 权限执行点击"
                "（保证可被急停取消、停止后不残留输入）。"
            ),
            "permissions": ["screen.capture_region", "input.mouse", "variables.write_declared"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "target_text": {"type": "string", "title": "查找文字",
                                    "description": "要找的文本，命中后点击其位置"},
                    "targets": {"type": "array", "items": {"type": "string"},
                                "title": "候选文字列表", "description": "多候选时按列表顺序取第一个命中"},
                    "match_mode": {"type": "string", "enum": ["contains", "equals", "regex"],
                                   "default": "contains", "title": "匹配方式"},
                    "score_threshold": {"type": "number", "minimum": 0, "maximum": 1,
                                        "default": 0.5, "title": "置信度阈值"},
                    "click_offset": {"type": "array", "items": {"type": "number"},
                                     "default": [0.5, 0.5], "title": "点击偏移(包围盒内比例)"},
                    "set_variable": {"type": "string", "title": "写入变量",
                                     "description": "把命中的文字写入声明过的变量"},
                    "image_path": {"type": "string", "title": "图片路径(独立模式)"},
                    "image_b64": {"type": "string", "title": "Base64图片(P3宿主注入)"},
                    "region_origin": {
                        "type": "array",
                        "items": {"type": "number"},
                        "title": "区域屏幕原点[x, y]",
                        "description": "P3 宿主注入：区域截图在屏幕上的左上角坐标；提供后 click_x/click_y 叠加为绝对屏幕坐标",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "matched_text": {"type": "string"},
                    "click_x": {"type": "number", "description": "区域局部 X 坐标（需宿主叠加区域原点）"},
                    "click_y": {"type": "number", "description": "区域局部 Y 坐标（需宿主叠加区域原点）"},
                    "coordinate_space": {"type": "string", "description": "absolute（已叠加区域原点）或 local（区域局部坐标，宿主须叠加）"},
                    "score": {"type": "number"},
                    "variable_value": {"type": "string"},
                    "words": {"type": "array"},
                },
            },
        },
        {
            "id": "ocr_read_region",
            "name": "OCR 读取区域文字",
            "description": "对指定区域做离线文字识别，返回全部识别到的文本与坐标，不点击。",
            "permissions": ["screen.capture_region"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "score_threshold": {"type": "number", "minimum": 0, "maximum": 1,
                                        "default": 0.5, "title": "置信度阈值"},
                    "image_path": {"type": "string", "title": "图片路径(独立模式)"},
                    "image_b64": {"type": "string", "title": "Base64图片(P3宿主注入)"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "text": {"type": "string"},
                    "words": {"type": "array"},
                },
            },
        },
    ],
}


def _check_actions_match_code() -> bool:
    """校验 MANIFEST_META 中声明的 action ids 与代码中 _ACTION_IDS 一致。

    使用 AST 解析避免相对导入问题（action.py 含 from .backend import ...）。
    """
    action_path = os.path.join(PLUGIN_ROOT, "src", "ocr_local", "action.py")
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
        "构建本地 OCR 插件",
        extra_check=_check_actions_match_code,
    )
