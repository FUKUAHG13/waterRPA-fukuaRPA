"""P5a 守卫测试：扫描区域引擎直写收口（统一经 engine.apply_scan_region）。

验证：
- RPAEngine.apply_scan_region 语义正确（集中设置 scan_region/scan_regions）
- SettingsController.apply_scan_region_config / on_regions_selected 经 engine.apply_scan_region 写入，
  而非直写引擎字段（满足 ARCHITECTURE.md :359「不得保存第二份状态 / 外部不得直接改引擎内部态」）
- 全代码库（除 engine.py 自身方法）无残留的 `.engine.scan_region =` 直写

这些测试均不依赖真实 Qt 窗口：RPAEngine 以 defer_backends=True 构造，
SettingsController 以普通 FakeHost 注入（其 __init__ 仅保存 weakref，无 Qt 依赖）。
"""

from __future__ import annotations

import pathlib
import re
import tempfile

from fukua_rpa.engine import RPAEngine
from fukua_rpa.ui.controllers.settings_controller import SettingsController


def _make_engine():
    return RPAEngine(base_dir=tempfile.mkdtemp(), defer_backends=True)


def test_engine_apply_scan_region_multi():
    e = _make_engine()
    e.apply_scan_region(scan_regions=[(1, 2, 3, 4), (5, 6, 7, 8)])
    assert e.scan_regions == [(1, 2, 3, 4), (5, 6, 7, 8)]
    assert e.scan_region is None


def test_engine_apply_scan_region_single_in_list():
    e = _make_engine()
    e.apply_scan_region(scan_regions=[(1, 2, 3, 4)])
    assert e.scan_regions == [(1, 2, 3, 4)]
    assert e.scan_region == (1, 2, 3, 4)


def test_engine_apply_scan_region_single_arg():
    e = _make_engine()
    e.apply_scan_region(scan_region=(1, 2, 3, 4))
    assert e.scan_regions == []
    assert e.scan_region == (1, 2, 3, 4)


def test_engine_apply_scan_region_empty():
    e = _make_engine()
    e.apply_scan_region()
    assert e.scan_regions == []
    assert e.scan_region is None


class _FakeEngine:
    def __init__(self):
        self.scan_region = None
        self.scan_regions = []
        self.calls = []

    def apply_scan_region(self, scan_region=None, scan_regions=None):
        self.calls.append((scan_region, scan_regions))
        if scan_regions:
            self.scan_regions = list(scan_regions)
            self.scan_region = scan_regions[0] if len(scan_regions) == 1 else None
        else:
            self.scan_regions = []
            self.scan_region = scan_region


class _FakeLabel:
    def setText(self, text):
        self.text = text


class _FakeHost:
    def __init__(self):
        self.engine = _FakeEngine()
        self.region_label = _FakeLabel()
        self.dirty = False
        self.logs = []

    def mark_profile_dirty(self):
        self.dirty = True

    def append_application_log(self, msg):
        self.logs.append(msg)


def test_settings_controller_apply_scan_region_config_single():
    host = _FakeHost()
    sc = SettingsController(host)
    sc.apply_scan_region_config(scan_region=(1, 2, 3, 4))
    assert sc.host.engine.calls == [((1, 2, 3, 4), None)]
    assert sc.host.engine.scan_region == (1, 2, 3, 4)
    assert sc.host.engine.scan_regions == []


def test_settings_controller_apply_scan_region_config_multi():
    host = _FakeHost()
    sc = SettingsController(host)
    sc.apply_scan_region_config(scan_regions=[(1, 2, 3, 4), (5, 6, 7, 8)])
    assert sc.host.engine.calls == [(None, [(1, 2, 3, 4), (5, 6, 7, 8)])]
    assert sc.host.engine.scan_region is None
    assert sc.host.engine.scan_regions == [(1, 2, 3, 4), (5, 6, 7, 8)]


def test_settings_controller_on_regions_selected():
    host = _FakeHost()
    sc = SettingsController(host)
    sc.on_regions_selected([(1, 2, 3, 4)])
    assert sc.host.engine.calls == [(None, [(1, 2, 3, 4)])]
    assert sc.host.dirty is True
    assert sc.host.engine.scan_region == (1, 2, 3, 4)


def test_no_direct_engine_scan_region_assignment():
    root = pathlib.Path(__file__).resolve().parents[1] / "fukua_rpa"
    pat = re.compile(r"\.?\bengine\b\.scan_regions?\s*=")
    hits = []
    for p in root.rglob("*.py"):
        if p.name == "engine.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p}:{i}: {line.strip()}")
    assert not hits, "发现残留的引擎字段直写（应统一经 engine.apply_scan_region）:\n" + "\n".join(hits)
