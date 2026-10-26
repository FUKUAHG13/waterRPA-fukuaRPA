"""P4f guard tests: bounded extraction of RPAWindow methods.

Verifies that the 12 region-selection / scheduler-plugin helpers were moved out
of ``RPAWindow`` into ``region_selection.py`` / ``plugin_helpers.py`` as plain
functions, while ``RPAWindow`` keeps thin delegates so the public API is
unchanged and behavior is preserved (extracted body == original method body).
"""
import ast
import hashlib
import os
import textwrap
from pathlib import Path

from scripts.private_release_inputs import (
    REFACTOR_BASELINE_REF,
    REFACTOR_BASELINE_SOURCE_PATH,
    load_refactor_baseline,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "fukua_rpa", "ui")
MW = os.path.join(UI, "main_window.py")
RS = os.path.join(UI, "region_selection.py")
PH = os.path.join(UI, "plugin_helpers.py")

GROUPS = {
    "region": [
        "open_region_selector", "open_multi_region_selector", "on_region_selected",
        "on_regions_selected", "normalize_region_list", "update_region_label",
        "apply_scan_region_config",
    ],
    "plugin": [
        "_plugin_usage_resolver", "_list_profiles_for_scheduler",
        "_plugin_enabled_status", "init_scheduler_and_plugins", "_build_tools_menu",
    ],
}
NAMES = [n for ns in GROUPS.values() for n in ns]
MODULE_FOR = {}
for g, ns in GROUPS.items():
    for n in ns:
        MODULE_FOR[n] = "region_selection" if g == "region" else "plugin_helpers"

# FR-007: 仍取同一个 P4f 提取前提交的原字节，不能改用当前方法体自比。
# 公开发行快照不携带私有开发历史，原Git文件作为已绑定指纹的私有验证输入；
# 缺失或内容变化必须失败，原12个方法的严格对照不变。
BASELINE_REF = REFACTOR_BASELINE_REF


def _class_methods(src, classname="RPAWindow"):
    tree = ast.parse(src)
    klass = next(n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == classname)
    return {b.name: b for b in klass.body
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _module_functions(path):
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, {b.name: b for b in tree.body
                 if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _dedent_method(src_lines, node):
    seg = src_lines[node.lineno - 1: node.end_lineno]
    return textwrap.dedent("".join(seg))


def test_delegates_inside_class():
    methods = _class_methods(Path(MW).read_text(encoding="utf-8"))
    # Batch 6 moved region/plugin delegates into FeedbackMiscForwarderMixin.
    FM = os.path.join(UI, "forwarders", "feedback_misc.py")
    mixin_methods = _class_methods(
        Path(FM).read_text(encoding="utf-8"), classname="FeedbackMiscForwarderMixin"
    )
    combined = {**mixin_methods, **methods}
    for nm in NAMES:
        assert nm in combined, f"{nm} missing from RPAWindow/Mixin"
        node = combined[nm]
        assert node.col_offset == 4, f"{nm} not indented as a class member"
        assert len(node.body) == 1, f"{nm} delegate should be a single statement"
        body_src = ast.unparse(node.body[0]).replace("\n", " ").strip()
        mod = MODULE_FOR[nm]
        assert body_src.startswith(
            (f"return {mod}.{nm}", f"yield from {mod}.{nm}",
             f"return await {mod}.{nm}")
        ), f"{nm} delegate body unexpected: {body_src}"


def test_baseline_source_has_fixed_historical_identity():
    """来源由原提交与原文件SHA锁定，不把私有origin误称为已公开历史。"""
    assert BASELINE_REF == "7ee4f99451ae236db5a224b0537e597b68f8794e"
    assert REFACTOR_BASELINE_SOURCE_PATH == "fukua_rpa/ui/main_window.py"
    assert hashlib.sha256(load_refactor_baseline().encode("utf-8")).hexdigest() == (
        "c5d0413a87492993060aec2c8b5d75f8785dfce1e029510f031e49a8d9c01641"
    )


def test_extracted_bodies_byte_equal_to_originals():
    # 基线取 P4f 提取前（P4f 提交的父）的 main_window：那时 12 个方法还是原始体。
    # P4f 提交后 HEAD 的 main_window 已变为薄委托，不能再作为"原始体"基线。
    # 下表列出 P4f 之后被"有意改写"的方法：它们不再字节等于原始体，
    # 改为验证改写后必须保留的收口/功能语义。未列出的方法仍要求严格字节相等。
    INTENTIONAL_REWRITES = {
        # P5a：直写引擎字段 → engine.apply_scan_region 收口。
        "on_region_selected": "engine.apply_scan_region",
        # P5b-2：RegionWindow 生命周期收口到 region_window_manager。
        "open_region_selector": "region_window_manager.open",
        # 自动连点器：启动时初始化 _auto_clicker_window 占位。
        "init_scheduler_and_plugins": "self._auto_clicker_window = None",
        # 自动连点器：工具菜单新增"自动连点器"入口。
        "_build_tools_menu": "show_auto_clicker_window",
    }
    # FR-007: 固定历史文件必须原字节可得；不能缺失时静默跳过或改用当前源码。
    head_src = load_refactor_baseline()
    head_methods = _class_methods(head_src)
    head_lines = head_src.splitlines(keepends=True)
    for nm in NAMES:
        mod = MODULE_FOR[nm]
        mpath = RS if mod == "region_selection" else PH
        _, mfuncs = _module_functions(mpath)
        if nm in INTENTIONAL_REWRITES:
            marker = INTENTIONAL_REWRITES[nm]
            src = Path(mpath).read_text(encoding="utf-8")
            assert marker in src, f"{nm} 有意改写后应保留语义标记 {marker!r}"
            continue
        orig = _dedent_method(head_lines, head_methods[nm])
        new = _dedent_method(
            Path(mpath).read_text(encoding="utf-8").splitlines(keepends=True),
            mfuncs[nm],
        )
        assert new == orig, f"{nm} extracted body != original method body"


def test_modules_importable_and_expose_functions():
    import fukua_rpa.ui.plugin_helpers as ph
    import fukua_rpa.ui.region_selection as rs
    for nm in GROUPS["region"]:
        assert hasattr(rs, nm), f"region_selection missing {nm}"
    for nm in GROUPS["plugin"]:
        assert hasattr(ph, nm), f"plugin_helpers missing {nm}"


def test_module_hygiene():
    rs_src, _ = _module_functions(RS)
    ph_src, _ = _module_functions(PH)
    # no duplicate logger line in either module
    assert rs_src.count("logger = logging.getLogger(__name__)") <= 1
    assert ph_src.count("logger = logging.getLogger(__name__)") <= 1
    # plugin_helpers uses logger -> must import logging
    if "logger." in ph_src:
        assert "import logging" in ph_src
