"""v1.0.57 缺陷复核报告中可运行时验证的修复项（对应 AGENTS.md 独立审查的「实际运行结果」与「测试覆盖」）。

覆盖：
* P1-1  overlay_image（until_cond{index}_overlay_image）未被枚举
* P1-4  插件清单严格校验拒绝 python_action / 未知字段
* P2-1  缺失模板图片被静默跳过（应失败）
* P2-4  GetForegroundWindow 的 ctypes restype 未配置导致句柄截断
* P3-1  screen_bounds 回退仅取主屏（应覆盖整个虚拟桌面）

P1-2 / P1-3 / P2-2 / P2-3 的逻辑正确性由独立只读审查线程按六方面核实；
P2-3 另由 plugins/*/tests/test_manifest_in_sync.py 的 --check / --validate 运行时覆盖。
"""

from __future__ import annotations

import ctypes
from unittest import mock

import pytest

from fukua_rpa.commands import IMAGE_TARGET_COMMANDS
from fukua_rpa.engine import RPAEngine
from fukua_rpa.plugin_manifest import PluginManifest, PluginManifestError
from fukua_rpa.task_images import iter_task_image_refs


def test_p1_1_overlay_image_enumerated():
    """覆盖匹配小图 until_cond1_overlay_image 必须被枚举，且紧跟其基础图。"""
    task = {
        "type": 15.0,
        "until_cond1_image": "a.png",
        "until_cond1_overlay_image": "b.png",
        "until_cond2_overlay_image": "c.png",
    }
    refs = list(iter_task_image_refs(task))
    keys = [k for (_kind, _c, k, _p) in refs]
    assert "until_cond1_image" in keys
    assert "until_cond1_overlay_image" in keys
    assert "until_cond2_overlay_image" in keys
    # overlay 必须紧跟其基础图之后，顺序不容错乱
    assert keys.index("until_cond1_overlay_image") == keys.index("until_cond1_image") + 1


def test_p1_4_plugin_manifest_python_action_and_underscore():
    """python_action 与下划线私有字段此前被严格校验拒绝，现应接受并往返保留。"""
    data = {
        "format": "fukuaRPA.plugin",
        "manifest_version": 1,
        "id": "demo-x",
        "name": "Demo",
        "version": "1.0.0",
        "author": "tester",
        "plugin_type": "python_action",
        "_note_p2": "dev only note",
        "api_version": 1,
        "min_app_version": "1.0.0",
        "entrypoint": "main.py",
        "permissions": [],
        "actions": [],
        "templates": [],
        "files": [],
    }
    manifest = PluginManifest.from_dict(data)
    assert manifest.plugin_type == "python_action"
    assert manifest.extra.get("_note_p2") == "dev only note"

    out = manifest.to_dict()
    assert out["_note_p2"] == "dev only note"  # 往返保留私有备注

    # 未知的非下划线字段仍应被拒绝
    bad_field = dict(data)
    bad_field["bogus"] = 1
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict(bad_field)

    # 非法 plugin_type 仍应被拒绝
    bad_type = dict(data)
    bad_type["plugin_type"] = "evil"
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict(bad_type)


def test_p2_4_get_foreground_window_restype():
    """GetForegroundWindow 必须配置 HWND restype，返回值才是完整句柄而非被截断的 32 位。"""
    user32 = ctypes.windll.user32
    assert user32.GetForegroundWindow.restype is ctypes.wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    assert isinstance(hwnd, int)


def test_p3_1_screen_bounds_virtual_desktop():
    """无 MSS 时回退应取整个虚拟桌面（含负原点多屏），而非仅主屏。"""
    engine = RPAEngine(defer_backends=True)
    engine._mss_disabled_for_run = True  # 强制走 Win32 回退路径
    metrics = {76: -1920, 77: 0, 78: 3840, 79: 1080}
    with mock.patch.object(
        ctypes.windll.user32, "GetSystemMetrics", lambda idx: metrics.get(idx, 0)
    ):
        bounds = engine.screen_bounds()
    assert bounds == (-1920, 0, 3840, 1080)


def test_p2_1_missing_template_fails_loudly():
    """引用的模板图片不存在时必须失败（返回 False），而非静默跳过。"""
    engine = RPAEngine(defer_backends=True)
    engine.opencv_available = True
    engine.max_scale = 1.0
    engine.scale_step = 0.1
    engine.scale_options_cache = {}

    cmd = next(iter(IMAGE_TARGET_COMMANDS))
    task = {"type": cmd, "value": "C:/no/such/template_abc123.png"}
    result = engine.load_and_precompute([task])
    assert result is False
