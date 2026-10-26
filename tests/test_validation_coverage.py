"""Targeted coverage tests for fukua_rpa.validation module.

Covers uncovered branches in TaskListValidator including:
- coord_sequence validation (lines 463-486)
- click task image validation (lines 507-535)
- flow task: screenshot, drag, auto_clicker (lines 593-659)
- coordinate step directions (lines 724-754)
- multi-image action rules (lines 766-857)
- until-condition task (lines 870-925)
- validate_task_list / validate_task_list_all wrappers (lines 936-961)
- jump fields, region fields, background click, image click point
- wait/wait_window/scroll validation
"""

import os
import tempfile
import unittest

from fukua_rpa.bulk_task_settings import filter_bulk_updates
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    UNTIL_CONDITION_MODES,
    UNTIL_LIMIT_ACTIONS,
)
from fukua_rpa.validation import (
    validate_task_list,
    validate_task_list_all,
)


def _cfg():
    """Return a fresh default profile config."""
    return default_profile_config()


def _click_task(value="100,200", **kw):
    """Build a left-click coordinate task dict."""
    task = {"type": 1.0, "value": value}
    task.update(kw)
    return task


class CoordSequenceValidationTests(unittest.TestCase):
    """Cover lines 462-486: coord_sequence_en branch."""

    def test_sequence_invalid_end_action(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="10,20;30,40",
            coord_sequence_end_action="无效动作",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("自定义点位结束后", error)

    def test_sequence_invalid_batch_mode(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="10,20;30,40",
            coord_sequence_batch_mode="invalid_mode",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("每次进入本步骤", error)

    def test_sequence_batch_count_invalid(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="10,20;30,40",
            coord_sequence_batch_mode="count",
            coord_sequence_batch_count="abc",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("批次数量", error)

    def test_sequence_batch_count_zero(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="10,20;30,40",
            coord_sequence_batch_mode="count",
            coord_sequence_batch_count="0",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("批次数量", error)

    def test_sequence_empty_points(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("至少要包含一个", error)

    def test_sequence_valid_passes(self):
        task = _click_task(
            coord_sequence_en=True,
            coord_sequence_points="10,20;30,40;50,60",
            coord_sequence_end_action="点完后循环",
            coord_sequence_batch_mode="all",
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class ClickTaskImageValidationTests(unittest.TestCase):
    """Cover lines 502-535: _validate_click_task image path branch."""

    def test_empty_value_rejected(self):
        task = {"type": 1.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_nonexistent_image_path(self):
        task = {"type": 1.0, "value": "Z:/nonexistent/img.png"}
        error = validate_task_list([task], _cfg())
        self.assertIn("图片路径不存在", error)

    def test_custom_confidence_out_of_range(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            # Write a minimal valid PNG (1x1 pixel)
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "custom_en": True,
                "custom_conf": "999",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("独立相似度", error)
        finally:
            os.unlink(img_path)

    def test_custom_scale_out_of_range(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "custom_en": True,
                "custom_conf": "0.8",
                "custom_scale_min": "0.001",
                "custom_scale_max": "99.0",
                "custom_scale_step": "0.05",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("独立缩放设置无效", error)
        finally:
            os.unlink(img_path)

    def test_valid_image_passes(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {"type": 1.0, "value": img_path}
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)

    def test_coordinate_value_passes_without_image_check(self):
        # coordinate value skips image validation entirely
        task = _click_task("500,300")
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class FlowTaskValidationTests(unittest.TestCase):
    """Cover lines 592-659: _validate_flow_task branches."""

    def test_secret_text_empty_value(self):
        task = {"type": 22.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("凭据名称", error)

    def test_launch_app_empty_value(self):
        task = {"type": 18.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_activate_window_empty_value(self):
        task = {"type": 20.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_close_window_empty_value(self):
        task = {"type": 21.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_screenshot_save_invalid_mode(self):
        task = {"type": 9.0, "value": "C:/", "capture_region_mode": "bad_mode"}
        error = validate_task_list([task], _cfg())
        self.assertIn("截图范围模式无效", error)

    def test_screenshot_save_custom_region_empty(self):
        task = {
            "type": 9.0,
            "value": "C:/",
            "capture_region_mode": "custom",
            "capture_region": "",
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("自定义截图区域", error)

    def test_screenshot_save_bad_extension(self):
        task = {"type": 9.0, "value": "C:/temp/output.txt"}
        error = validate_task_list([task], _cfg())
        self.assertIn("截图保存路径", error)

    def test_screenshot_save_file_path_nonexistent_parent(self):
        task = {"type": 9.0, "value": "Z:/no_such_dir_xyz/shot.png"}
        error = validate_task_list([task], _cfg())
        self.assertIn("截图保存目录不存在", error)

    def test_screenshot_save_valid_directory(self):
        with tempfile.TemporaryDirectory() as td:
            task = {"type": 9.0, "value": td}
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)

    def test_screenshot_save_valid_file_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "shot.png")
            task = {"type": 9.0, "value": path}
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)

    def test_drag_missing_arrow(self):
        task = {"type": 10.0, "value": "100,100 200,200"}
        error = validate_task_list([task], _cfg())
        self.assertIn("->", error)

    def test_drag_bad_coordinates(self):
        task = {"type": 10.0, "value": "abc -> def"}
        error = validate_task_list([task], _cfg())
        self.assertIn("拖拽坐标格式异常", error)

    def test_drag_invalid_duration(self):
        task = {"type": 10.0, "value": "100,100 -> 200,200", "recorded_duration": "999"}
        error = validate_task_list([task], _cfg())
        self.assertIn("录制拖拽时长", error)

    def test_drag_target_img_not_exists(self):
        task = {
            "type": 10.0,
            "value": "100,100 -> 200,200",
            "drag_target_img": "Z:/no_img.png",
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("拖拽终点图片不存在", error)

    def test_drag_source_img_not_exists(self):
        task = {
            "type": 11.0,
            "value": "100,100 -> 200,200",
            "drag_source_img": "Z:/no_img.png",
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("拖拽起点图片不存在", error)

    def test_drag_valid(self):
        task = {"type": 10.0, "value": "100,100 -> 200,200"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_auto_clicker_fixed_mode_missing_coords(self):
        task = {
            "type": 28.0,
            "value": "",
            "ac_center_mode": "fixed",
            "ac_fixed_x": "",
            "ac_fixed_y": "",
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("连点器", error)

    def test_auto_clicker_fixed_mode_valid(self):
        task = {
            "type": 28.0,
            "value": "",
            "ac_center_mode": "fixed",
            "ac_fixed_x": "500",
            "ac_fixed_y": "300",
        }
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_auto_clicker_fixed_mode_rejects_non_finite_coords(self):
        """非有限坐标必须被预运行校验拒绝（FR-20261012-001）。

        ``inf``/``nan``/``1e999`` 能通过 ``float()``，运行期却被解析为
        ``None`` 并静默改为“跟随鼠标”模式，用户指定的点击点被悄悄换掉。
        """
        for raw in ("inf", "-inf", "nan", "1e999"):
            with self.subTest(raw=raw):
                task = {
                    "type": 28.0,
                    "value": "",
                    "ac_center_mode": "fixed",
                    "ac_fixed_x": raw,
                    "ac_fixed_y": raw,
                }
                error = validate_task_list([task], _cfg())
                self.assertIsNotNone(error, f"{raw!r} 必须被预运行校验拒绝")
                self.assertIn("连点器", error)

    def test_auto_clicker_fixed_mode_accepts_finite_bounds_and_rejects_overflow(self):
        """合法有限坐标（含上界）继续放行；超出上界报错；跟随鼠标模式不受影响。"""
        for raw in ("0", "-1000000", "1000000", "500.5"):
            with self.subTest(raw=raw):
                task = {
                    "type": 28.0,
                    "value": "",
                    "ac_center_mode": "fixed",
                    "ac_fixed_x": raw,
                    "ac_fixed_y": raw,
                }
                self.assertIsNone(
                    validate_task_list([task], _cfg()),
                    f"合法坐标 {raw!r} 必须继续放行",
                )

        overflow = {
            "type": 28.0,
            "value": "",
            "ac_center_mode": "fixed",
            "ac_fixed_x": "1000001",
            "ac_fixed_y": "0",
        }
        self.assertIsNotNone(validate_task_list([overflow], _cfg()))

        follow_mouse = {"type": 28.0, "value": "", "ac_center_mode": "current"}
        self.assertIsNone(validate_task_list([follow_mouse], _cfg()))

    def test_popup_notify_empty_ok(self):
        task = {"type": 12.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_stop_run_empty_ok(self):
        task = {"type": 13.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_sound_alert_empty_ok(self):
        task = {"type": 14.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class WaitTaskValidationTests(unittest.TestCase):
    """Cover lines 549-581: _validate_wait_task branches."""

    def test_wait_empty_value(self):
        task = {"type": 5.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_wait_invalid_number(self):
        task = {"type": 5.0, "value": "abc"}
        error = validate_task_list([task], _cfg())
        self.assertIn("等待时间", error)

    def test_wait_out_of_range(self):
        task = {"type": 5.0, "value": "99999"}
        error = validate_task_list([task], _cfg())
        self.assertIn("等待时间", error)

    def test_wait_valid(self):
        task = {"type": 5.0, "value": "2.5"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_scroll_invalid_number(self):
        task = {"type": 6.0, "value": "not_a_number"}
        error = validate_task_list([task], _cfg())
        self.assertIn("有限数字", error)

    def test_scroll_valid(self):
        task = {"type": 6.0, "value": "-3"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_wait_window_empty_value(self):
        task = {"type": 19.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_wait_window_invalid_timeout(self):
        task = {"type": 19.0, "value": "notepad", "window_timeout": "99999"}
        error = validate_task_list([task], _cfg())
        self.assertIn("窗口等待时间", error)

    def test_wait_window_valid(self):
        task = {"type": 19.0, "value": "notepad", "window_timeout": "30"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class CoordinateStepDirectionTests(unittest.TestCase):
    """Cover lines 724-754: _validate_coordinate_step direction branches."""

    def _task_with_step(self, **kw):
        base = _click_task(
            coord_step_en=True,
            coord_step_every="1",
            coord_step_direction="向下",
            coord_step_distance="10",
            coord_step_dx="0",
            coord_step_dy="0",
            coord_step_point="",
            coord_step_max_steps="0",
            coord_step_max_distance="0",
            coord_step_reset_after="0",
        )
        base.update(kw)
        return base

    def test_invalid_max_steps(self):
        task = self._task_with_step(coord_step_max_steps="abc")
        error = validate_task_list([task], _cfg())
        self.assertIn("最大偏移次数", error)

    def test_invalid_reset_after(self):
        task = self._task_with_step(coord_step_reset_after="-1")
        error = validate_task_list([task], _cfg())
        self.assertIn("重置循环", error)

    def test_invalid_max_distance(self):
        task = self._task_with_step(
            coord_step_direction="向下", coord_step_max_distance="bad"
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("最大偏移距离", error)

    def test_direction_up_invalid_distance(self):
        task = self._task_with_step(
            coord_step_direction="向上", coord_step_distance="nan"
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("步进距离", error)

    def test_direction_left_valid(self):
        task = self._task_with_step(
            coord_step_direction="向左", coord_step_distance="5"
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_direction_right_valid(self):
        task = self._task_with_step(
            coord_step_direction="向右", coord_step_distance="5"
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_custom_offset_invalid(self):
        task = self._task_with_step(
            coord_step_direction="自定义偏移",
            coord_step_dx="bad",
            coord_step_dy="0",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("自定义偏移 dx/dy", error)

    def test_custom_offset_valid(self):
        task = self._task_with_step(
            coord_step_direction="自定义偏移",
            coord_step_dx="3",
            coord_step_dy="-2",
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_move_to_point_invalid(self):
        task = self._task_with_step(
            coord_step_direction="移动到新点位",
            coord_step_point="not_coord",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("目标点位", error)

    def test_move_to_point_max_steps_one(self):
        task = self._task_with_step(
            coord_step_direction="移动到新点位",
            coord_step_point="200,300",
            coord_step_max_steps="1",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("路径点位数", error)

    def test_move_to_point_valid(self):
        task = self._task_with_step(
            coord_step_direction="移动到新点位",
            coord_step_point="200,300",
            coord_step_max_steps="5",
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_invalid_direction(self):
        task = self._task_with_step(coord_step_direction="斜着走")
        error = validate_task_list([task], _cfg())
        self.assertIn("步进方向", error)


class JumpFieldsValidationTests(unittest.TestCase):
    """Cover lines 361-390: _validate_jump_fields error branches."""

    def test_success_skip_non_integer(self):
        task = _click_task(success_skip="abc")
        error = validate_task_list([task], _cfg())
        self.assertIn("成功后跳过", error)

    def test_fail_skip_non_integer(self):
        task = _click_task(fail_skip="1.5")
        error = validate_task_list([task], _cfg())
        self.assertIn("失败后跳过", error)

    def test_success_jump_out_of_range(self):
        task = _click_task(success_jump="99")
        error = validate_task_list([task], _cfg())
        self.assertIn("成功后跳至", error)

    def test_fail_jump_non_integer(self):
        task = _click_task(fail_jump="xyz")
        error = validate_task_list([task], _cfg())
        self.assertIn("失败后跳至", error)

    def test_call_step_out_of_range(self):
        task = _click_task(call_step="50")
        error = validate_task_list([task], _cfg())
        self.assertIn("先执行指定步骤", error)

    def test_call_step_self_reference(self):
        task = _click_task(call_step="1")
        error = validate_task_list([task], _cfg())
        self.assertIn("不能把自己", error)

    def test_fail_limit_invalid(self):
        task = _click_task(fail_limit="0")
        error = validate_task_list([task], _cfg())
        self.assertIn("连续失败次数", error)

    def test_repeat_count_invalid(self):
        task = _click_task(repeat_mode="指定次数", repeat_count="0")
        error = validate_task_list([task], _cfg())
        self.assertIn("重复次数", error)

    def test_run_max_executions_invalid(self):
        task = _click_task(run_max_executions="abc")
        error = validate_task_list([task], _cfg())
        self.assertIn("本次运行最多执行", error)

    def test_loop_start_invalid(self):
        task = _click_task(step_loop_start="0")
        error = validate_task_list([task], _cfg())
        self.assertIn("循环范围起始", error)

    def test_loop_end_invalid(self):
        task = _click_task(step_loop_end="-1")
        error = validate_task_list([task], _cfg())
        self.assertIn("循环范围停止", error)

    def test_loop_end_less_than_start(self):
        task = _click_task(step_loop_start="5", step_loop_end="2")
        error = validate_task_list([task], _cfg())
        self.assertIn("不能小于起始循环", error)

    def test_point_limit_count_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {"type": 1.0, "value": img_path, "point_limit_en": True, "point_limit_count": "abc"}
            error = validate_task_list([task], _cfg())
            self.assertIn("同点点击上限", error)
        finally:
            os.unlink(img_path)


class RegionFieldsValidationTests(unittest.TestCase):
    """Cover lines 405-412: _validate_region_fields."""

    def test_region_bad_format(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "step_region_en": True,
                "step_region": "bad_format",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("本步识别区域", error)
        finally:
            os.unlink(img_path)

    def test_region_valid_passes(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "step_region_en": True,
                "step_region": "0,0,200,200",
            }
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)


class BackgroundClickValidationTests(unittest.TestCase):
    """Cover lines 293-318: background_click_en branch."""

    def test_background_click_empty_binding(self):
        task = _click_task(background_click_en=True, background_click_binding={})
        error = validate_task_list([task], _cfg())
        self.assertIn("尚未选择目标窗口", error)

    def test_background_click_incomplete_binding(self):
        task = _click_task(
            background_click_en=True,
            background_click_binding={"source_x": 100},
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("窗口绑定不完整", error)

    def test_background_click_source_mismatch(self):
        task = _click_task(
            "100,200",
            background_click_en=True,
            background_click_binding={
                "source_x": 999,
                "source_y": 888,
                "root_client_x": 10,
                "root_client_y": 20,
            },
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("坐标已在选择窗口后发生变化", error)

    def test_background_click_valid(self):
        task = _click_task(
            "100,200",
            background_click_en=True,
            background_click_binding={
                "source_x": 100,
                "source_y": 200,
                "root_client_x": 10,
                "root_client_y": 20,
            },
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class ImageClickPointValidationTests(unittest.TestCase):
    """Cover lines 319-328: image_click_point_en branch."""

    def test_click_point_nonexistent_image(self):
        # image_click_point_en is kept for image-based clicks;
        # a non-existent file triggers the os.path.isfile check
        task = {
            "type": 1.0,
            "value": "Z:/no_such_image.png",
            "image_click_point_en": True,
            "image_click_point_rx": "0.5",
            "image_click_point_ry": "0.5",
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("图片内点击点", error)

    def test_click_point_bad_ratio(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "image_click_point_en": True,
                "image_click_point_rx": "2.0",
                "image_click_point_ry": "0.5",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("图片内点击点", error)
        finally:
            os.unlink(img_path)


class ConfirmClickDelayValidationTests(unittest.TestCase):
    """Cover lines 286-291: image_confirm_click / delayed_second_click delay."""

    def test_confirm_click_delay_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "image_confirm_click_en": True,
                "image_confirm_click_delay": "999",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("识别确认补点延时", error)
        finally:
            os.unlink(img_path)

    def test_delayed_second_click_delay_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "delayed_second_click_en": True,
                "delayed_second_click_delay": "bad",
            }
            error = validate_task_list([task], _cfg())
            self.assertIn("延时二次点击延时", error)
        finally:
            os.unlink(img_path)


class MultiImageActionValidationTests(unittest.TestCase):
    """Cover lines 764-857: _validate_multi_image_action."""

    def _mia_task(self, rules, **kw):
        task = {"type": 26.0, "value": "", "multi_image_actions": rules}
        task.update(kw)
        return task

    def test_rules_not_a_list(self):
        task = self._mia_task("not_a_list")
        error = validate_task_list([task], _cfg())
        self.assertIn("规则列表格式无效", error)

    def test_too_many_rules(self):
        rules = [{"image": "x.png", "action": "left_click"} for _ in range(33)]
        task = self._mia_task(rules)
        error = validate_task_list([task], _cfg())
        self.assertIn("最多允许", error)

    def test_rule_not_a_mapping(self):
        task = self._mia_task(["not_a_dict"])
        error = validate_task_list([task], _cfg())
        self.assertIn("包含无效规则", error)

    def test_empty_rules_after_normalize(self):
        task = self._mia_task([])
        error = validate_task_list([task], _cfg())
        self.assertIn("至少要添加一条规则", error)

    def test_rule_image_not_exists(self):
        rules = [{"image": "Z:/no_such.png", "action": "left_click"}]
        task = self._mia_task(rules)
        error = validate_task_list([task], _cfg())
        self.assertIn("图片路径不存在", error)

    def test_rule_invalid_action(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "fly_away"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("命中动作无效", error)
        finally:
            os.unlink(img_path)

    def test_rule_bad_region(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click", "region": "bad"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("识别区域格式错误", error)
        finally:
            os.unlink(img_path)

    def test_rule_bad_confidence(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click", "confidence": "99"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("相似度必须在", error)
        finally:
            os.unlink(img_path)

    def test_rule_required_value_missing(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "text", "value": ""}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("需要填写动作参数", error)
        finally:
            os.unlink(img_path)

    def test_rule_wait_invalid_seconds(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "wait", "value": "999999"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("等待时间必须在", error)
        finally:
            os.unlink(img_path)

    def test_rule_scroll_invalid_value(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "scroll", "value": "nan"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIn("滚轮参数必须是有限数字", error)
        finally:
            os.unlink(img_path)

    def test_custom_scale_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click"}]
            task = self._mia_task(
                rules,
                custom_en=True,
                custom_scale_min="0.001",
                custom_scale_max="99",
                custom_scale_step="0.05",
            )
            error = validate_task_list([task], _cfg())
            self.assertIn("独立缩放设置无效", error)
        finally:
            os.unlink(img_path)

    def test_valid_multi_image_action(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click"}]
            task = self._mia_task(rules)
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)


class UntilTaskValidationTests(unittest.TestCase):
    """Cover lines 868-925: _validate_until_task."""

    def _until_task(self, **kw):
        task = {
            "type": 15.0,
            "value": "",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,100,100",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        task.update(kw)
        return task

    def test_no_conditions_enabled(self):
        task = self._until_task(
            until_cond1_en=False,
            until_cond2_en=False,
            until_cond3_en=False,
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("至少要启用一个条件", error)

    def test_invalid_logic(self):
        task = self._until_task(until_logic="随便")
        error = validate_task_list([task], _cfg())
        self.assertIn("条件关系无效", error)

    def test_invalid_limit_action(self):
        task = self._until_task(until_on_limit="爆炸")
        error = validate_task_list([task], _cfg())
        self.assertIn("达到上限后处理方式无效", error)

    def test_invalid_false_jump(self):
        task = self._until_task(until_false_jump="99")
        error = validate_task_list([task], _cfg())
        self.assertIn("未满足跳回", error)

    def test_invalid_true_jump(self):
        task = self._until_task(until_true_jump="abc")
        error = validate_task_list([task], _cfg())
        self.assertIn("满足后跳至", error)

    def test_invalid_max_checks(self):
        task = self._until_task(until_max_checks="-1")
        error = validate_task_list([task], _cfg())
        self.assertIn("最多检查次数", error)

    def test_invalid_max_seconds(self):
        task = self._until_task(until_max_seconds="bad")
        error = validate_task_list([task], _cfg())
        self.assertIn("最多等待秒数", error)

    def test_image_mode_missing_image(self):
        task = self._until_task(
            until_cond1_mode="图片出现",
            until_cond1_image="",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("图片路径不存在", error)

    def test_bad_region_format(self):
        task = self._until_task(
            until_cond1_mode="区域发生变化",
            until_cond1_region="bad_region",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("区域格式错误", error)

    def test_region_mode_requires_region(self):
        task = self._until_task(
            until_cond1_mode="区域发生变化",
            until_cond1_region="",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("必须填写或框选区域", error)

    # -- FR-20260930-012：越界条件模式必须被拒绝，不得静默降级 --------------

    def test_invalid_condition_mode_is_rejected(self):
        """越界 mode 必须在预运行校验阶段报错。

        FR-20260930-012：修复前 `until_condition_list_from_data` 会把越界值
        静默改写为「图片出现」，校验随后只报「图片路径不存在」（或缺图时直接
        放行），用户无从得知模式已被替换。
        """
        task = self._until_task(until_cond1_mode="区域变化")
        error = validate_task_list([task], _cfg())
        self.assertIsNotNone(error)
        self.assertIn("模式无效", error)
        self.assertIn("区域变化", error)

    def test_every_documented_condition_mode_passes_the_domain_check(self):
        """六值域内每个取值都不得被判为「模式无效」（防过度拒绝）。"""
        for mode in UNTIL_CONDITION_MODES:
            with self.subTest(mode=mode):
                task = self._until_task(
                    until_cond1_mode=mode,
                    until_cond1_region="0,0,100,100",
                )
                error = validate_task_list([task], _cfg())
                if error is not None:
                    self.assertNotIn("模式无效", error)

    def test_absent_condition_mode_uses_the_parser_default(self):
        """未写 mode 键时按解析器默认「图片出现」处理，不得判为模式无效。

        解析器 `until_condition_list_from_data` 对缺失键回退「图片出现」；
        校验必须使用同源默认值，否则会把合法的旧方案误判为模式无效。
        """
        task = self._until_task(until_cond1_region="0,0,100,100")
        task.pop("until_cond1_mode")
        error = validate_task_list([task], _cfg())
        if error is not None:
            self.assertNotIn("模式无效", error)

    def test_condition_confidence_out_of_range(self):
        task = self._until_task(
            until_cond1_mode="区域发生变化",
            until_cond1_region="0,0,100,100",
            until_cond1_conf="99",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("图片相似度", error)

    def test_condition_diff_out_of_range(self):
        task = self._until_task(
            until_cond1_mode="区域发生变化",
            until_cond1_region="0,0,100,100",
            until_cond1_diff="200",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("变化阈值", error)

    def test_condition_similarity_out_of_range(self):
        task = self._until_task(
            until_cond1_mode="区域变成指定图片",
            until_cond1_region="0,0,100,100",
            until_cond1_similarity="200",
        )
        # Need a valid image for this mode
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task["until_cond1_image"] = img_path
            error = validate_task_list([task], _cfg())
            self.assertIn("区域相似度", error)
        finally:
            os.unlink(img_path)

    def test_valid_until_task(self):
        task = self._until_task(
            until_cond1_mode="区域发生变化",
            until_cond1_region="0,0,100,100",
            until_logic="全部满足",
            until_on_limit="继续下一步",
        )
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_image_mode_with_valid_image(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = self._until_task(
                until_cond1_mode="图片出现",
                until_cond1_image=img_path,
                until_cond1_region="",
            )
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)


class UntilOverlayAndStabilityValidationTests(unittest.TestCase):
    """FR-SCOPE013-001 / FR-SCOPE013-002：覆盖匹配必填项与稳定秒数/子区域取值域。

    这些字段在修复前于 `fukua_rpa/validation.py` 中**零匹配**（双检索确证），非法或
    缺失输入被直接放行：覆盖匹配缺小图/缺子区域时消费侧恒返回 False（条件静默恒假），
    `stable_seconds` 非法值运行期取 0.0（首帧即判稳定），`sub_region` 非法文本运行期
    退化为整区域比较。三者的共同判据都是「预运行校验必须拒绝并给出可读错误」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.overlay_image = os.path.join(self._tmp.name, "overlay.png")
        _write_test_png(self.overlay_image, width=32, height=32)

    def _overlay_task(self, **kw):
        task = {
            "type": 15.0,
            "value": "",
            "until_cond1_en": True,
            "until_cond1_mode": "覆盖匹配",
            "until_cond1_region": "0,0,100,100",
            "until_cond1_overlay_image": self.overlay_image,
            "until_cond1_overlay_rect": "0,0,20,20",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        task.update(kw)
        return task

    def _stable_task(self, **kw):
        task = {
            "type": 15.0,
            "value": "",
            "until_cond1_en": True,
            "until_cond1_mode": "区域不变",
            "until_cond1_region": "0,0,100,100",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        task.update(kw)
        return task

    # --- FR-SCOPE013-001：覆盖匹配 ---

    def test_overlay_mode_missing_region_rejected(self):
        error = validate_task_list([self._overlay_task(until_cond1_region="")], _cfg())
        self.assertIn("必须填写或框选区域", error)

    def test_overlay_mode_empty_overlay_image_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_image="")], _cfg()
        )
        self.assertIn("覆盖小图路径不存在", error)

    def test_overlay_mode_nonexistent_overlay_image_rejected(self):
        missing = os.path.join(self._tmp.name, "does-not-exist.png")
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_image=missing)], _cfg()
        )
        self.assertIn("覆盖小图路径不存在", error)

    def test_overlay_mode_missing_overlay_rect_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_rect="")], _cfg()
        )
        self.assertIn("子区域位置", error)

    def test_overlay_mode_invalid_overlay_rect_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_rect="not-a-region")], _cfg()
        )
        self.assertIn("子区域位置", error)

    def test_overlay_mode_sub_rect_outside_region_rejected(self):
        """FR-20261005-101: 子区域越出大区域必须在校验阶段报错（否则运行期恒假）。"""
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_rect="90,90,20,20")], _cfg()
        )
        self.assertIsNotNone(error)
        self.assertIn("必须完全落在大区域内", error)

    def test_overlay_mode_sub_rect_at_region_edge_is_accepted(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_rect="80,80,20,20")], _cfg()
        )
        self.assertIsNone(error)

    def test_overlay_mode_scale_out_of_range_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_scale="9.0")], _cfg()
        )
        self.assertIn("覆盖小图缩放", error)

    def test_overlay_mode_scale_non_numeric_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_scale="abc")], _cfg()
        )
        self.assertIn("覆盖小图缩放", error)

    def test_overlay_mode_threshold_out_of_range_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_threshold="2")], _cfg()
        )
        self.assertIn("覆盖匹配阈值", error)

    def test_overlay_mode_threshold_non_numeric_rejected(self):
        error = validate_task_list(
            [self._overlay_task(until_cond1_overlay_threshold="abc")], _cfg()
        )
        self.assertIn("覆盖匹配阈值", error)

    def test_overlay_mode_valid_config_passes(self):
        task = self._overlay_task(
            until_cond1_overlay_scale="1.0",
            until_cond1_overlay_threshold="0.8",
        )
        self.assertIsNone(validate_task_list([task], _cfg()))

    def test_overlay_scale_domain_matches_shared_constants(self):
        """覆盖小图缩放/阈值的取值域必须与共享常量一致（单一来源守护）。

        常量在 `fukua_rpa/constants.py` 中定义、被 `validation.py` 与
        `engine/conditions.py` 共同消费；此处断言其字面值等于本文件其余用例使用的
        文档取值域，避免任一侧被单独改动而漂移（docs/adr/0025）。
        """
        from fukua_rpa.constants import (
            UNTIL_OVERLAY_SCALE_DEFAULT,
            UNTIL_OVERLAY_SCALE_MAX,
            UNTIL_OVERLAY_SCALE_MIN,
            UNTIL_OVERLAY_THRESHOLD_DEFAULT,
        )

        self.assertEqual(
            (UNTIL_OVERLAY_SCALE_MIN, UNTIL_OVERLAY_SCALE_MAX), (0.1, 5.0)
        )
        self.assertEqual(UNTIL_OVERLAY_SCALE_DEFAULT, 1.0)
        self.assertEqual(UNTIL_OVERLAY_THRESHOLD_DEFAULT, 0.8)

    def test_overlay_mode_clamp_boundaries_pass(self):
        # 取值域边界必须与 engine/conditions.py 的 clamp 口径一致（constants 单一来源）。
        for field, value in (
            ("until_cond1_overlay_scale", "0.1"),
            ("until_cond1_overlay_scale", "5.0"),
            ("until_cond1_overlay_threshold", str(CONFIDENCE_MIN)),
            ("until_cond1_overlay_threshold", str(CONFIDENCE_MAX)),
        ):
            with self.subTest(field=field, value=value):
                task = self._overlay_task(**{field: value})
                self.assertIsNone(validate_task_list([task], _cfg()))

    def test_overlay_mode_below_clamp_boundaries_rejected(self):
        for field, value in (
            ("until_cond1_overlay_scale", "0.05"),
            ("until_cond1_overlay_threshold", str(CONFIDENCE_MIN / 2)),
        ):
            with self.subTest(field=field, value=value):
                task = self._overlay_task(**{field: value})
                self.assertIsNotNone(validate_task_list([task], _cfg()))

    # --- FR-SCOPE013-002：stable_seconds / sub_region ---

    def test_stable_seconds_non_numeric_rejected(self):
        error = validate_task_list(
            [self._stable_task(until_cond1_stable_seconds="abc")], _cfg()
        )
        self.assertIn("稳定秒数", error)

    def test_stable_seconds_negative_rejected(self):
        error = validate_task_list(
            [self._stable_task(until_cond1_stable_seconds="-5")], _cfg()
        )
        self.assertIn("稳定秒数", error)

    def test_stable_seconds_valid_values_pass(self):
        for value in ("0", "2.5"):
            with self.subTest(value=value):
                task = self._stable_task(until_cond1_stable_seconds=value)
                self.assertIsNone(validate_task_list([task], _cfg()))

    def test_sub_region_invalid_text_rejected(self):
        task = {
            "type": 15.0,
            "value": "",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,100,100",
            "until_cond1_sub_region": "not-a-region",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        error = validate_task_list([task], _cfg())
        self.assertIn("子区域格式错误", error)

    def test_sub_region_empty_and_valid_pass(self):
        for value in ("", "10,10,20,20"):
            with self.subTest(value=value):
                task = {
                    "type": 15.0,
                    "value": "",
                    "until_cond1_en": True,
                    "until_cond1_mode": "区域发生变化",
                    "until_cond1_region": "0,0,100,100",
                    "until_cond1_sub_region": value,
                    "until_cond2_en": False,
                    "until_cond3_en": False,
                }
                self.assertIsNone(validate_task_list([task], _cfg()))

    # --- FR-SCOPE013-003 / FR-SCOPE001-001：旧取值兼容 ---

    def test_legacy_until_on_limit_failure_action_still_loads(self):
        """常量域保留「按失败处理」，旧方案加载与运行不得报错。

        界面已不再提供该选项（见 tests/test_task_settings_controllers.py），但
        `UNTIL_LIMIT_ACTIONS` 不得删除该取值，否则已保存的旧方案会被校验器拒绝。
        """
        self.assertIn("按失败处理", UNTIL_LIMIT_ACTIONS)
        task = self._stable_task(until_on_limit="按失败处理")
        self.assertIsNone(validate_task_list([task], _cfg()))

    def test_unknown_until_on_limit_still_rejected(self):
        task = self._stable_task(until_on_limit="爆炸")
        error = validate_task_list([task], _cfg())
        self.assertIn("达到上限后处理方式无效", error)

    # --- FR-SCOPE013-004：批量写入不得绕过校验 ---

    def test_bulk_update_cannot_bypass_overlay_validation(self):
        """批量设置放行 `until_*` 前缀字段后，仍必须被预运行校验拦住。

        `filter_bulk_updates` 只做「字段是否适用」的能力过滤，不做取值域校验；
        前缀规则放行本身不是缺陷，缺陷是放行后写入的非法值此前无人拒绝。
        """
        task = self._overlay_task()
        updates = {
            "until_cond1_overlay_rect": "not-a-region",
            "until_cond1_stable_seconds": "abc",
        }
        applied = filter_bulk_updates(task, updates)
        self.assertEqual(set(applied), set(updates))

        task.update(applied)
        self.assertIsNotNone(validate_task_list([task], _cfg()))


class WrapperFunctionTests(unittest.TestCase):
    """Cover lines 929-961: validate_task_list and validate_task_list_all."""

    def test_validate_task_list_invalid_config(self):
        error = validate_task_list([{"type": 1.0, "value": "1,2"}], "not_a_mapping")
        self.assertIn("运行配置不是有效的配置对象", error)

    def test_validate_task_list_invalid_tasks_type(self):
        error = validate_task_list("not_a_sequence", _cfg())
        self.assertIn("步骤列表格式错误", error)

    def test_validate_task_list_all_invalid_config(self):
        errors = validate_task_list_all([{"type": 1.0, "value": "1,2"}], None)
        self.assertEqual(len(errors), 1)
        self.assertIn("运行配置不是有效的配置对象", errors[0])

    def test_validate_task_list_all_invalid_tasks(self):
        errors = validate_task_list_all(b"bytes", _cfg())
        self.assertEqual(len(errors), 1)
        self.assertIn("步骤列表格式错误", errors[0])

    def test_validate_task_list_all_empty_tasks(self):
        errors = validate_task_list_all([], _cfg())
        self.assertIn("没有可执行步骤", errors[0])

    def test_validate_task_list_all_multiple_errors(self):
        tasks = [
            {"type": 5.0, "value": ""},
            {"type": 18.0, "value": ""},
        ]
        errors = validate_task_list_all(tasks, _cfg())
        self.assertEqual(len(errors), 2)

    def test_validate_task_list_exception_caught(self):
        # Trigger an internal exception via overflow
        task = {"type": 1.0, "value": "1,2", "success_jump": "9" * 400}
        error = validate_task_list([task], _cfg())
        # Should return an error string, not raise
        self.assertIsInstance(error, str)


class ValidateAllEdgeCaseTests(unittest.TestCase):
    """Cover lines 139-161: validate_all edge cases."""

    def test_non_mapping_task_in_list(self):
        tasks = ["not_a_dict", _click_task()]
        errors = validate_task_list_all(tasks, _cfg())
        self.assertTrue(any("不是有效的步骤对象" in e for e in errors))

    def test_all_disabled_steps(self):
        tasks = [{"type": 1.0, "value": "1,2", "step_disabled": True}]
        errors = validate_task_list_all(tasks, _cfg())
        self.assertTrue(any("没有已启用的步骤" in e for e in errors))

    def test_debug_breakpoint_invalid_condition(self):
        task = _click_task(debug_breakpoint=True, debug_condition="invalid(((")
        error = validate_task_list([task], _cfg())
        self.assertIn("断点条件", error)

    def test_debug_breakpoint_valid_condition(self):
        task = _click_task(debug_breakpoint=True, debug_condition="x > 1")
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class SetVariableAndExpressionTests(unittest.TestCase):
    """Cover lines 681-692: set_variable and expression validation."""

    def test_set_variable_invalid(self):
        task = {"type": 16.0, "value": "=== bad assignment ==="}
        error = validate_task_list([task], _cfg())
        self.assertIn("设置变量", error)

    def test_set_variable_valid(self):
        task = {"type": 16.0, "value": "x = 42"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)

    def test_expression_invalid(self):
        task = {"type": 17.0, "value": "if(("}
        error = validate_task_list([task], _cfg())
        self.assertIn("判断表达式", error)

    def test_expression_valid(self):
        task = {"type": 17.0, "value": "x > 0"}
        error = validate_task_list([task], _cfg())
        self.assertIsNone(error)


class PluginActionValidationTests(unittest.TestCase):
    """Cover lines 229-233: plugin action validation."""

    def test_plugin_action_invalid(self):
        # A plugin action with no plugin_id should fail
        task = {"type": 27.0, "value": "", "plugin_id": ""}
        error = validate_task_list([task], _cfg())
        # FR-c01-001: 原断言 "error is None or '插件动作' in error" 为恒真析取，
        # 任何实现结果都通过。改为锁定失败分支：必须返回含"插件动作"的错误。
        self.assertIsNotNone(error)
        self.assertIn("插件动作", error)


class RunConfigErrorTests(unittest.TestCase):
    """Cover lines 149-151: RunConfigError early return."""

    def test_bad_config_returns_error(self):
        cfg = _cfg()
        cfg["conf"] = "not_a_number"
        task = _click_task()
        errors = validate_task_list_all([task], cfg)
        self.assertTrue(any("全局相似度" in e for e in errors))


class BackgroundClickBindingIntTests(unittest.TestCase):
    """Cover lines 312-313: background_click binding int() conversion failure."""

    def test_binding_non_integer_values(self):
        task = _click_task(
            "100,200",
            background_click_en=True,
            background_click_binding={
                "source_x": "abc",
                "source_y": "def",
                "root_client_x": "ghi",
                "root_client_y": "jkl",
            },
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("窗口绑定不完整", error)


class ClickTaskCustomScaleSuccessTests(unittest.TestCase):
    """Cover lines 525-526: custom scale success path in _validate_click_task."""

    def test_valid_custom_scale(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = {
                "type": 1.0,
                "value": img_path,
                "custom_en": True,
                "custom_conf": "0.8",
                "custom_scale_min": "0.9",
                "custom_scale_max": "1.1",
                "custom_scale_step": "0.05",
                "custom_gray": True,
            }
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)


class FlatColorTemplateTests(unittest.TestCase):
    """Cover line 534: template_detail_status rejection in click task."""

    def test_flat_color_image_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_flat_png(img_path, width=64, height=64)
            task = {"type": 1.0, "value": img_path}
            error = validate_task_list([task], _cfg())
            self.assertIn("模板无法安全识别", error)
        finally:
            os.unlink(img_path)


class FlowTaskEmptyValueTests(unittest.TestCase):
    """Cover line 606: empty value for non-exempt flow task."""

    def test_left_drag_empty_value(self):
        task = {"type": 10.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)

    def test_right_drag_empty_value(self):
        task = {"type": 11.0, "value": ""}
        error = validate_task_list([task], _cfg())
        self.assertIn("参数不能为空", error)


class CoordinateStepExtraTests(unittest.TestCase):
    """Cover lines 732, 744: max_distance negative and dy invalid."""

    def _task_with_step(self, **kw):
        base = _click_task(
            coord_step_en=True,
            coord_step_every="1",
            coord_step_direction="向下",
            coord_step_distance="10",
            coord_step_dx="0",
            coord_step_dy="0",
            coord_step_point="",
            coord_step_max_steps="0",
            coord_step_max_distance="0",
            coord_step_reset_after="0",
        )
        base.update(kw)
        return base

    def test_max_distance_negative(self):
        task = self._task_with_step(
            coord_step_direction="向下", coord_step_max_distance="-5"
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("最大偏移距离", error)

    def test_custom_offset_dy_invalid(self):
        task = self._task_with_step(
            coord_step_direction="自定义偏移",
            coord_step_dx="3",
            coord_step_dy="bad",
        )
        error = validate_task_list([task], _cfg())
        self.assertIn("自定义偏移 dx/dy", error)


class MultiImageCustomScaleSuccessTests(unittest.TestCase):
    """Cover lines 786, 789: multi-image custom scale success path."""

    def test_valid_custom_scale_multi_image(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click"}]
            task = {
                "type": 26.0,
                "value": "",
                "multi_image_actions": rules,
                "custom_en": True,
                "custom_scale_min": "0.9",
                "custom_scale_max": "1.1",
                "custom_scale_step": "0.05",
                "custom_gray": False,
            }
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)


class MultiImageTemplateDetailTests(unittest.TestCase):
    """Cover lines 849, 857: multi-image template_detail_status failure."""

    def test_flat_color_image_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_flat_png(img_path, width=64, height=64)
            rules = [{"image": img_path, "action": "left_click"}]
            task = {"type": 26.0, "value": "", "multi_image_actions": rules}
            error = validate_task_list([task], _cfg())
            self.assertIn("模板无法安全识别", error)
        finally:
            os.unlink(img_path)


class UntilExtraTests(unittest.TestCase):
    """Cover lines 887, 916, 921, 923-925: until max_seconds negative and image template."""

    def _until_task(self, **kw):
        task = {
            "type": 15.0,
            "value": "",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,100,100",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        task.update(kw)
        return task

    def test_max_seconds_negative(self):
        task = self._until_task(until_max_seconds="-1")
        error = validate_task_list([task], _cfg())
        self.assertIn("最多等待秒数", error)

    def test_image_disappear_mode_valid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = self._until_task(
                until_cond1_mode="图片消失",
                until_cond1_image=img_path,
                until_cond1_region="",
            )
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)

    def test_region_become_image_mode_valid(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_test_png(img_path, width=64, height=64)
            task = self._until_task(
                until_cond1_mode="区域变成指定图片",
                until_cond1_image=img_path,
                until_cond1_region="0,0,100,100",
            )
            error = validate_task_list([task], _cfg())
            self.assertIsNone(error)
        finally:
            os.unlink(img_path)

    def test_image_mode_flat_color_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name
        try:
            _write_flat_png(img_path, width=64, height=64)
            task = self._until_task(
                until_cond1_mode="图片出现",
                until_cond1_image=img_path,
                until_cond1_region="",
            )
            error = validate_task_list([task], _cfg())
            self.assertIn("模板不安全", error)
        finally:
            os.unlink(img_path)


class WrapperExceptionTests(unittest.TestCase):
    """Cover lines 941-942, 960-961: exception handling in wrappers."""

    def test_validate_task_list_catches_internal_error(self):
        from unittest.mock import patch

        with patch(
            "fukua_rpa.validation.EngineRunConfig.from_mapping",
            side_effect=ValueError("internal test error"),
        ):
            error = validate_task_list([_click_task()], _cfg())
        self.assertIn("步骤配置格式错误", error)

    def test_validate_task_list_all_catches_internal_error(self):
        from unittest.mock import patch

        with patch(
            "fukua_rpa.validation.EngineRunConfig.from_mapping",
            side_effect=ValueError("internal test error"),
        ):
            errors = validate_task_list_all([_click_task()], _cfg())
        self.assertEqual(len(errors), 1)
        self.assertIn("步骤配置格式错误", errors[0])


def _write_flat_png(path, width=64, height=64):
    """Write a flat-color PNG (rejected by template_detail_status)."""
    import struct
    import zlib

    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = []
    for _y in range(height):
        row = b"\x00"  # filter byte
        for _x in range(width):
            row += b"\x80\x80\x80"  # flat gray
        raw_rows.append(row)
    raw_data = b"".join(raw_rows)
    compressed = zlib.compress(raw_data)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr_data))
        f.write(_chunk(b"IDAT", compressed))
        f.write(_chunk(b"IEND", b""))


def _write_test_png(path, width=64, height=64):
    """Write a minimal valid PNG with texture (not flat color)."""
    import struct
    import zlib

    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # IDAT - create textured image (checkerboard pattern)
    raw_rows = []
    for y in range(height):
        row = b"\x00"  # filter byte
        for x in range(width):
            if (x + y) % 2 == 0:
                row += b"\xff\x80\x40"
            else:
                row += b"\x20\x60\xa0"
        raw_rows.append(row)
    raw_data = b"".join(raw_rows)
    compressed = zlib.compress(raw_data)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr_data))
        f.write(_chunk(b"IDAT", compressed))
        f.write(_chunk(b"IEND", b""))


class TargetIdReferenceValidationTests(unittest.TestCase):
    """FR-301-001: 预运行校验必须核对权威 *_target_id 引用（#45），
    悬空引用不得放行后在运行期被静默清除跳转。"""

    def test_dangling_success_target_id_rejected(self):
        tasks = [
            _click_task(step_id="s-1", success_target_id="s-999"),
            _click_task(step_id="s-2"),
        ]
        error = validate_task_list(tasks, _cfg())
        self.assertIsNotNone(error)
        self.assertIn("成功后跳至", error)
        self.assertIn("s-999", error)

    def test_dangling_fail_target_id_rejected(self):
        tasks = [
            _click_task(step_id="s-1", fail_target_id="ghost-id"),
            _click_task(step_id="s-2"),
        ]
        error = validate_task_list(tasks, _cfg())
        self.assertIsNotNone(error)
        self.assertIn("失败后跳至", error)

    def test_valid_target_id_passes(self):
        tasks = [
            _click_task(step_id="s-1", success_target_id="s-2"),
            _click_task(step_id="s-2"),
        ]
        error = validate_task_list(tasks, _cfg())
        self.assertIsNone(error)

    def test_legacy_numeric_only_without_target_id_unaffected(self):
        # 旧数据只有数字分支字段、无 target_id：仍按数字校验（越界报错）。
        tasks = [
            _click_task(step_id="s-1", success_jump="9"),
        ]
        error = validate_task_list(tasks, _cfg())
        self.assertIsNotNone(error)
        self.assertIn("成功后跳至", error)


if __name__ == "__main__":
    unittest.main()
