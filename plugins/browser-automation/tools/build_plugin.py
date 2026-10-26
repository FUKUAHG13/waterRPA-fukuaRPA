#!/usr/bin/env python3
"""构建「浏览器自动化」插件：生成 manifest.json（含逐文件 SHA-256）并打包成 .fkrplugin。

用法
----
    python tools/build_plugin.py            # 生成 manifest.json + browser-automation.fkrplugin
    python tools/build_plugin.py --no-zip   # 只生成 manifest.json，不打包
    python tools/build_plugin.py --validate # 用原项目契约严格校验（需能 import 到 fukua_rpa.plugin_manifest）
    python tools/build_plugin.py --check    # 校验磁盘 manifest.json 与源码生成逻辑一致（防漂移）

约定
----
* 本脚本只读写 plugins/browser-automation/ 自身目录，绝不修改原 fukuaRPA 项目。
* 打包内容 = manifest.json + 运行时文件（src/、README.md、requirements.txt、LICENSE）。
  tools/ 与 tests/ 属开发期产物，不进可分发包。
* 共享构建逻辑见 plugins/tools/common.py；本文件只保留插件特有的运行时文件清单与清单元数据。
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
    "src/browser_automation/__init__.py",
    "src/browser_automation/action.py",
    "src/browser_automation/backend.py",
    "src/browser_automation/engine.py",
    "src/browser_automation/models.py",
]

MANIFEST_META = {
    "format": "fukuaRPA.plugin",
    "manifest_version": 1,
    "id": "browser-automation",
    "name": "浏览器自动化（网页 DOM 操作）",
    "version": "1.1.0",
    "author": "fukuaRPA 社区插件",
    "plugin_type": "python_action",
    "_note_p2": "P2 阶段：浏览器由插件自起 Playwright 驱动；顶层 permissions 恒为各动作 permissions 的并集，P3 宿主迁移时按动作实际需要扩充",
    "api_version": 1,
    "min_app_version": "1.0.0",
    "entrypoint": "src/browser_automation/action.py:run",
    # 信任边界：联网行为来自用户主动启动的浏览器子进程（process.launch_confirmed），
    # fukuaRPA 主程序本身不对外通信。v1 权限枚举无 network 项，本插件也不申请。
    # INVARIANT：本列表必须恰好等于下方所有 action.permissions 的并集，由
    # plugins/tools/common.py 的 --validate 门禁强制。顶层权限是安装确认页与
    # 插件中心的能力摘要，多列会让用户高估本插件（曾经预留的 window.query /
    # window.control 无任何动作申请，已移除），少列会掩盖真实能力
    # （browser_screenshot 需要的 files.user_selected 曾被遗漏，已补齐）。
    # P3 迁移到宿主窗口能力时，先给具体动作加权限，再由门禁驱动此处同步。
    "permissions": [
        "process.launch_confirmed",
        "screen.capture_region",
        "files.user_selected",
        "variables.write_declared",
    ],
    # 注意：manifest 的 dependencies 仅用于「插件间依赖」（需为插件 id），
    # pip 运行时依赖写在 requirements.txt，不在此声明。
    "dependencies": [],
    "templates": [],
    "actions": [
        {
            "id": "browser_navigate",
            "name": "浏览器导航到网址",
            "description": "启动（或复用）浏览器并导航到指定 URL，返回落地地址。",
            "permissions": ["process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "title": "网址"},
                    "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "default": "load"},
                    "headless": {"type": "boolean", "default": True},
                    "timeout_ms": {"type": "integer", "minimum": 1000, "default": 30000},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "found": {"type": "boolean"}},
            },
        },
        {
            "id": "browser_click",
            "name": "点击网页元素",
            "description": "按 文本/CSS/XPath 定位元素并点击，返回该元素信息（含包围盒坐标）。",
            "permissions": ["process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["text", "css", "xpath"], "default": "text"},
                    "value": {"type": "string", "title": "文本或选择器"},
                    "index": {"type": "integer", "default": 0},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "element": {"type": "object"}},
            },
        },
        {
            "id": "browser_fill",
            "name": "填写输入框",
            "description": "按定位方式找到输入框并填入文本（用于表单自动化）。",
            "permissions": ["process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["text", "css", "xpath"], "default": "text"},
                    "value": {"type": "string", "title": "文本或选择器"},
                    "text": {"type": "string", "title": "要填入的文本"},
                    "index": {"type": "integer", "default": 0},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "element": {"type": "object"}},
            },
        },
        {
            "id": "browser_extract_text",
            "name": "提取网页文本",
            "description": "提取整页或某元素的可见文本（用于读取页面内容/校验）。",
            "permissions": ["process.launch_confirmed", "variables.write_declared"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["text", "css", "xpath"]},
                    "value": {"type": "string", "title": "可选：定位某元素"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "text": {"type": "string"}},
            },
        },
        {
            "id": "browser_wait_for",
            "name": "等待元素出现",
            "description": "轮询等待指定元素出现，超时返回 found=false（用于异步页面）。",
            "permissions": ["process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["text", "css", "xpath"], "default": "text"},
                    "value": {"type": "string", "title": "文本或选择器"},
                    "timeout_ms": {"type": "integer", "minimum": 1000, "default": 5000},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}},
            },
        },
        {
            "id": "browser_get_attribute",
            "name": "读取元素属性",
            "description": "读取第 index 个匹配元素的某个属性值（如 href、value、id）。",
            "permissions": ["process.launch_confirmed", "variables.write_declared"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["text", "css", "xpath"], "default": "text"},
                    "value": {"type": "string", "title": "文本或选择器"},
                    "attr": {"type": "string", "title": "属性名，如 href"},
                    "index": {"type": "integer", "default": 0},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "value": {"type": "string"}},
            },
        },
        {
            "id": "browser_screenshot",
            "name": "网页截图",
            "description": "截取整页或某元素并保存到指定路径，返回实际保存路径。",
            "permissions": ["process.launch_confirmed", "screen.capture_region", "files.user_selected"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "保存路径"},
                    "by": {"type": "string", "enum": ["text", "css", "xpath"]},
                    "value": {"type": "string", "title": "可选：截取某元素"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"found": {"type": "boolean"}, "path": {"type": "string"}},
            },
        },
    ],
}



def _check_actions_match_code() -> bool:
    """校验 MANIFEST_META 中声明的 action ids 与代码中 _ACTION_IDS 一致。

    使用 AST 解析避免相对导入问题（action.py 含 from .backend import ...）。
    """
    action_path = os.path.join(PLUGIN_ROOT, "src", "browser_automation", "action.py")
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
        "构建浏览器自动化插件",
        extra_check=_check_actions_match_code,
    )
