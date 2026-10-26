#!/usr/bin/env python3
"""构建「屏幕录制 / 视频取证」插件：生成 manifest.json（含逐文件 SHA-256）并打包成 .fkrplugin。

用法
----
    python tools/build_plugin.py            # 生成 manifest.json + screen-recorder.fkrplugin
    python tools/build_plugin.py --no-zip   # 只生成 manifest.json，不打包
    python tools/build_plugin.py --validate # 用原项目契约严格校验（需能 import 到 fukua_rpa.plugin_manifest）
    python tools/build_plugin.py --check    # 校验磁盘 manifest.json 与源码生成逻辑一致（防漂移）

约定
----
* 本脚本只读写 plugins/screen-recorder/ 自身目录，绝不修改原 fukuaRPA 项目。
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
    "src/screen_recorder/__init__.py",
    "src/screen_recorder/action.py",
    "src/screen_recorder/backend.py",
    "src/screen_recorder/engine.py",
    "src/screen_recorder/models.py",
]

MANIFEST_META = {
    "format": "fukuaRPA.plugin",
    "manifest_version": 1,
    "id": "screen-recorder",
    "name": "屏幕录制与视频取证",
    "version": "1.1.0",
    "author": "fukuaRPA 社区插件",
    "plugin_type": "python_action",
    "_note_p2": "P2 阶段：当前为 Python 实现（python_action），非 WASM 沙箱",
    "api_version": 1,
    "min_app_version": "1.0.0",
    "entrypoint": "src/screen_recorder/action.py:run",
    # 信任边界：屏幕录制/截图为纯本地行为，不涉及任何对外网络通信，
    # fukuaRPA 主程序本身不对外通信。v1 权限枚举无 network 项，本插件也不申请。
    "permissions": [
        "screen.capture_region",
        "files.user_selected",
        "files.plugin_private_data",
        "process.launch_confirmed",
    ],
    # 注意：manifest 的 dependencies 仅用于「插件间依赖」（需为插件 id），
    # pip 运行时依赖写在 requirements.txt，不在此声明。
    "dependencies": [],
    "templates": [],
    "actions": [
        {
            "id": "recorder_start",
            "name": "开始屏幕录制",
            "description": "开始录制屏幕（可选区域、帧率、音轨）。默认用 mss+opencv 软编码后端，纯离线、支持暂停/恢复。",
            "permissions": ["screen.capture_region", "files.user_selected", "files.plugin_private_data", "process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "title": "视频保存路径（含扩展名）"},
                    "output_dir": {"type": "string", "title": "输出目录（自动命名，插件私有目录优先）"},
                    "format": {"type": "string", "enum": ["mp4", "avi"], "default": "mp4"},
                    "fps": {"type": "integer", "minimum": 1, "maximum": 60, "default": 15},
                    "region": {"type": "object", "title": "录制区域 {x,y,width,height}，省略则全屏"},
                    "audio": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否录制音轨。仅 ffmpeg 后端支持音频录制；默认 mss 后端忽略此参数。",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"recording": {"type": "boolean"}, "output_path": {"type": "string"}},
            },
        },
        {
            "id": "recorder_pause",
            "name": "暂停录制",
            "description": "暂停当前录制（仅 mss 后端支持；ffmpeg 后端会明确报错）。",
            "permissions": ["screen.capture_region"],
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {
                "type": "object",
                "properties": {"recording": {"type": "boolean"}, "paused": {"type": "boolean"}},
            },
        },
        {
            "id": "recorder_resume",
            "name": "恢复录制",
            "description": "从暂停状态恢复录制（仅 mss 后端支持）。",
            "permissions": ["screen.capture_region"],
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {
                "type": "object",
                "properties": {"recording": {"type": "boolean"}, "paused": {"type": "boolean"}},
            },
        },
        {
            "id": "recorder_stop",
            "name": "停止录制并取证",
            "description": "停止录制，返回输出路径、时长、帧数与文件大小，用于视频取证归档。",
            "permissions": ["screen.capture_region", "files.plugin_private_data"],
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {
                "type": "object",
                "properties": {
                    "recording": {"type": "boolean"},
                    "output_path": {"type": "string"},
                    "duration_ms": {"type": "integer"},
                    "frames": {"type": "integer"},
                    "file_bytes": {"type": "integer"},
                },
            },
        },
        {
            "id": "recorder_snapshot",
            "name": "屏幕单帧截图",
            "description": "抓取当前屏幕（可选区域）保存为 PNG，独立于录制会话，常用于取证留证。",
            "permissions": ["screen.capture_region", "files.user_selected", "files.plugin_private_data", "process.launch_confirmed"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "title": "截图保存路径（.png）"},
                    "output_dir": {"type": "string", "title": "输出目录（自动命名）"},
                    "region": {"type": "object", "title": "截图区域 {x,y,width,height}，省略则全屏"},
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    ],
}



def _check_actions_match_code() -> bool:
    """校验 MANIFEST_META 中声明的 action ids 与代码中 _ACTION_IDS 一致。

    使用 AST 解析避免相对导入问题（action.py 含 from .backend import ...）。
    """
    action_path = os.path.join(PLUGIN_ROOT, "src", "screen_recorder", "action.py")
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
        "构建屏幕录制插件",
        extra_check=_check_actions_match_code,
    )
