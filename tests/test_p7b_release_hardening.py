"""P7-2 文档/发布加固守卫测试（K/L/M 收尾项）。

覆盖：
- R-7 凭据 ACL 收敛：atomic_write_json(restrict_acl=True) 对目标文件施加 0o600。
- R-6 stderr 脱敏：global_exception_handler 仅输出已脱敏安全报告，不泄露原始异常消息。
- R-4 loop_val 封顶：指定次数模式超过上限抛 RunConfigError。
- C-3 audio 措辞与默认行为一致：screen-recorder manifest 的 audio 参数描述声明 ffmpeg 专用 / mss 忽略。
- C-8 显隐矩阵三artifact一致性：文档三向规则 + 能力表(SETTING_FIELD_CAPABILITY) + 显隐测试存在且交叉引用一致。
- R-3 DPI 回退非静默：coordinates.py 在 DPI 回退路径记录 warning（实机确认另记）。
"""
import io
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_atomic_write_json_restrict_acl(tmp_path):
    from fukua_rpa.io_utils import atomic_write_json

    target = tmp_path / "secret.json"
    atomic_write_json(target, {"k": "v"}, restrict_acl=True)
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "v"}
    # 权限收敛：非 Windows 下应为 0o600（仅属主可读写）。
    if sys.platform != "win32":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"期望 0o600，实际 {oct(mode)}"
    # 不带 restrict_acl 也应正常写入（向后兼容）。
    other = tmp_path / "plain.json"
    atomic_write_json(other, {"a": 1})
    assert json.loads(other.read_text(encoding="utf-8")) == {"a": 1}


def test_global_exception_handler_only_safe_report():
    from fukua_rpa.logging_service import global_exception_handler

    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        try:
            raise ValueError("SECRET_MARKER_XYZ_123")
        except ValueError:
            global_exception_handler(*sys.exc_info())
    finally:
        sys.stderr = old
    out = buf.getvalue()
    # 安全报告头存在
    assert "!!! 严重崩溃 !!!" in out
    # 原始异常消息不应泄露（safe_exception_report 不含消息体；且不再调用 sys.__excepthook__）
    assert "SECRET_MARKER_XYZ_123" not in out


def test_loop_val_capped():
    from fukua_rpa.run_config import RunConfigError, _parse_loop_fields

    # 合法值通过
    ok = _parse_loop_fields({"loop_mode": "指定次数", "loop_val": "100"}, task_count=None)
    assert ok["loop_val"] == 100.0
    # 超过上限（R-4 封顶 1_000_000）抛错
    with pytest.raises(RunConfigError):
        _parse_loop_fields(
            {"loop_mode": "指定次数", "loop_val": "2000000"}, task_count=None
        )


# FR-011: 工作树为 loop_start_round / loop_end_round 新增了与 loop_val 同口径的
# 1e6 上限（run_config.py:318-337，ENG-FR-001 的输入面收敛），但全测试目录
# 无任何断言守护——未来重构可能无声移除该防线，重新打开"等待循环范围长期
# 空转"的输入面。以下两条用例把该上限钉死。
def test_loop_start_round_capped():
    from fukua_rpa.run_config import RunConfigError, _parse_loop_fields

    # loop_mode 取"指定次数"：单次模式下 loop_start_round > 1 会先被另一条
    # 语义校验拦截，无法验证上限本身。
    ok = _parse_loop_fields(
        {"loop_mode": "指定次数", "loop_start_round": "100", "loop_end_round": "200"},
        task_count=None,
    )
    assert ok["loop_start_round"] == 100
    # 超过 1e6 必须抛错
    with pytest.raises(RunConfigError):
        _parse_loop_fields(
            {
                "loop_mode": "指定次数",
                "loop_start_round": "1000001",
                "loop_end_round": "0",
            },
            task_count=None,
        )


def test_loop_end_round_capped():
    from fukua_rpa.run_config import RunConfigError, _parse_loop_fields

    ok = _parse_loop_fields(
        {"loop_start_round": "1", "loop_end_round": "1000000"}, task_count=None
    )
    assert ok["loop_end_round"] == 1000000
    with pytest.raises(RunConfigError):
        _parse_loop_fields(
            {"loop_start_round": "1", "loop_end_round": "1000001"}, task_count=None
        )


def test_audio_capability_wording_consistent():
    manifest = ROOT / "plugins" / "screen-recorder" / "manifest.json"
    if not manifest.exists():
        pytest.skip("screen-recorder 插件 manifest 不存在（子模块/未检出）")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    # 找到任意提及 audio/音频 的描述串（结构无关，避免耦合 manifest 具体层级）
    desc = ""

    def walk(node):
        nonlocal desc
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            low = node.lower()
            if ("audio" in low or "音频" in node) and not desc:
                desc = node

    walk(data)
    assert desc, "manifest 未找到 audio 相关描述"
    # C-3：措辞须声明音频录制仅 ffmpeg 后端支持、默认 mss 后端忽略（与默认行为一致）
    assert "ffmpeg" in desc, "audio 描述应声明 ffmpeg 后端支持"
    assert ("mss" in desc) or ("忽略" in desc), "audio 描述应声明默认 mss 后端忽略"


def test_visibility_matrix_three_way_consistency():
    from fukua_rpa.bulk_task_settings import SETTING_FIELD_CAPABILITY

    doc = ROOT / "docs" / "步骤设置显隐矩阵.md"
    # 该文档当前受版本控制（提交 26b7f56 起重新纳入），三向同步规则必须始终校验。
    text = doc.read_text(encoding="utf-8")
    # 三向规则：文档自身要求同步更新能力表、矩阵、显隐测试
    assert "能力表" in text and "显隐矩阵" in text and "显隐测试" in text
    assert "同时更新能力表" in text, "文档未声明三向同步更新规则"
    # 矩阵已填充（含动作行）
    assert "左键单击" in text and "截图保存" in text

    # 能力表 artifact：SETTING_FIELD_CAPABILITY 非空（doc 中“高级设置”项需在能力表有对应键）
    assert isinstance(SETTING_FIELD_CAPABILITY, dict) and SETTING_FIELD_CAPABILITY
    doc_token_to_field = {
        "反色匹配": "inverse_match_en",
        "同点点击上限": "point_limit_count",
        "本步识别区域": "step_region",
        "多图动作": "multi_image_actions",
        "识别确认补点": "image_confirm_click_en",
        "延时二次点击": "delayed_second_click_en",
        "生成式坐标步进": "coord_step_en",
    }
    for _doc_label, field_key in doc_token_to_field.items():
        assert field_key in SETTING_FIELD_CAPABILITY, (
            f"显隐矩阵提到的“{_doc_label}”在能力表无对应字段 {field_key}"
        )

    # 显隐测试 artifact：可见性测试文件存在且可导入
    inv_test = ROOT / "tests" / "test_invariant_hidden_step.py"
    ctrl_test = ROOT / "tests" / "test_task_settings_controllers.py"
    assert inv_test.exists(), "隐步执行不变式测试缺失"
    assert ctrl_test.exists(), "显隐控制器测试缺失"
    import importlib.util

    for t in (inv_test, ctrl_test):
        spec = importlib.util.spec_from_file_location(t.stem, t)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod is not None


def test_visibility_matrix_action_rows_match_capability_table():
    """FR-20261008-901：矩阵「动作专属设置」行必须与能力表实际一致。

    上一个用例只做文本包含与「键存在」断言，因此矩阵动作行与
    ``step_settings_capabilities`` 的漂移（``REG-20261008-001`` 漏记、
    ``REG-20261008-002`` 错误补记）两次都只能靠人工探针发现。本用例把矩阵四类
    目标行翻译成能力表断言，使该类漂移由门禁直接拦截。
    """
    from fukua_rpa.commands import (
        CLICK_COMMANDS,
        DRAG_COMMANDS,
        SETTING_COORDINATE_PATH,
        SETTING_IMAGE_RECOGNITION,
        step_settings_capabilities,
    )

    text = (ROOT / "docs" / "步骤设置显隐矩阵.md").read_text(encoding="utf-8")
    for row_label in ("（图片）", "（坐标）", "（目标为空）", "左键拖拽、右键拖拽"):
        assert row_label in text, f"显隐矩阵缺少动作行：{row_label}"

    click = sorted(CLICK_COMMANDS)[0]
    drag = sorted(DRAG_COMMANDS)[0]

    # 图片目标：图片设置适用，坐标路径不适用。
    image_caps = step_settings_capabilities(click, "C:/img.png")
    assert SETTING_IMAGE_RECOGNITION in image_caps
    assert SETTING_COORDINATE_PATH not in image_caps

    # 坐标目标：坐标路径适用，图片设置不适用。
    coord_caps = step_settings_capabilities(click, "100,200")
    assert SETTING_COORDINATE_PATH in coord_caps
    assert SETTING_IMAGE_RECOGNITION not in coord_caps

    # 目标为空：两者都不适用（草稿保留但被有效副本屏蔽）。
    empty_caps = step_settings_capabilities(click, "")
    assert SETTING_COORDINATE_PATH not in empty_caps
    assert SETTING_IMAGE_RECOGNITION not in empty_caps

    # 拖拽：普通坐标步进不适用（矩阵「左键拖拽、右键拖拽」行）。
    drag_caps = step_settings_capabilities(drag, "C:/img.png")
    assert SETTING_COORDINATE_PATH not in drag_caps


def test_dpi_fallback_not_silent():
    coords_src = (ROOT / "fukua_rpa" / "coordinates.py").read_text(encoding="utf-8")
    # R-3：DPI 回退路径必须记录 warning（非静默）；实机混合 DPI 偏移确认另列人工门禁。
    assert "回退到 GetSystemMetrics/逻辑坐标路径" in coords_src
    assert "logger.warning" in coords_src
