# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Serialize per-step settings controls into the workflow task mapping."""

import ast
import fnmatch
import functools
import inspect

from ..commands import command_code
from ..constants import TASK_TYPE_SOUND_ALERT
from ..task_model import (
    serialize_coord_step_manual_points,
    serialize_coordinate_sequence,
)

#: Widget accessor suffixes ``collect_task_settings`` uses, mapped to themselves.
#: Reading a widget always goes through one of these four calls, so the attribute
#: name alone identifies the widget without a hand-written lookup table.
_WIDGET_READERS = frozenset({"text", "isChecked", "currentText", "rules"})


def _attr_chains(node):
    """Every ``self.<widget>.<reader>()`` chain appearing under *node*."""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute) or func.attr not in _WIDGET_READERS:
            continue
        owner = func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            found.append(owner.attr)
    return found


@functools.lru_cache(maxsize=1)
def _field_widget_map():
    """Map each collected field name to the widget attributes that produce it.

    FR-20261006-008: the bulk-edit path needs to know which fields the user can
    currently see. Deriving that map **from this very function's source** is the
    point: a hand-written field->widget table is exactly what drifts from the
    collector, and the drift shows up as hidden fields being silently overwritten.
    """
    tree = ast.parse(inspect.getsource(collect_task_settings))
    mapping: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Dict):
            pairs = [
                (key, value)
                # ``strict=True``: a dict literal whose keys and values differ in
                # length cannot be constructed by Python itself, so a mismatch here
                # means the AST is not what we assume — truncating it would silently
                # drop a field from the visibility map.
                for key, value in zip(node.keys, node.values, strict=True)
                if key is not None
            ]
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            # ``data["ac_jitter_v"] = self.ac_jitter_v_edit.text()`` writes a
            # single field outside any literal; a dict-literal-only scan misses it.
            target = node.targets[0]
            if isinstance(target, ast.Subscript):
                pairs = [(target.slice, node.value)]
        for key, value in pairs:
            name = _field_name(key)
            if not name:
                continue
            widgets = set(_attr_chains(value))
            if widgets:
                mapping.setdefault(name, set()).update(widgets)
    return mapping


def _field_name(key):
    """The field-name pattern for a dict key node, or None if it is not a name.

    Per-condition fields are written as f-strings (``f"until_cond{idx}_conf"``).
    They are kept as a *pattern*: every concrete field collected at runtime must
    match it, otherwise ``dialog_visible_setting_keys`` would silently report a
    conditional field as having no widget — and therefore treat it as invisible,
    dropping it from the bulk write.
    """
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    if isinstance(key, ast.JoinedStr):
        parts = []
        for value in key.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("*")
        return "".join(parts)
    return None


def dialog_visible_setting_keys(dialog, fields=None):
    """Field names whose editor is actually visible in *dialog* right now.

    A field counts as visible when at least one of its widgets is visible. Simple
    mode hides advanced-only editors; the bulk "apply to differing fields" option
    must not touch those, otherwise checking it rewrites values the user cannot
    see or change.

    Args:
        dialog: The live settings dialog.
        fields: Optional concrete field names to resolve. Defaults to every pattern
            the collector defines. Passing the collected keys resolves the
            f-string patterns (``until_cond*_conf``) to the fields actually present.

    Fields with no discoverable widget are reported as **visible**: this function
    narrows the bulk write, so an unmappable field must fail open rather than be
    silently dropped from the user's explicit "apply" action.
    """
    mapping = _field_widget_map()
    if fields is None:
        fields = list(mapping)
    visible = set()
    for field in fields:
        widgets = _resolve_widgets(mapping, field)
        if not widgets:
            visible.add(field)
            continue
        for name in widgets:
            try:
                widget = getattr(dialog, name, None)
            except RuntimeError:
                # Attribute access on a deleted C++ object raises RuntimeError,
                # which getattr's default does **not** swallow.
                continue
            if widget is None:
                continue
            try:
                # ``isHidden()`` — not ``isVisible()``. The latter also depends on
                # whether the whole dialog has been shown, so a dialog still being
                # built (or running offscreen in tests) reports *everything* hidden
                # and the bulk write would silently stop covering advanced fields.
                # What we need is "is this control explicitly hidden in the current
                # mode", which is exactly what ``isHidden()`` answers.
                if not widget.isHidden():
                    visible.add(field)
                    break
            except RuntimeError:
                # A widget whose C++ object was deleted is not visible.
                continue
    return visible


def _resolve_widgets(mapping, field):
    """Widget names for *field*, matching f-string patterns such as ``a_*_b``."""
    widgets = mapping.get(field)
    if widgets:
        return widgets
    resolved: set[str] = set()
    for pattern, names in mapping.items():
        if "*" in pattern and fnmatch.fnmatchcase(field, pattern):
            resolved |= names
    return resolved


def collect_task_settings(dialog):
    self = dialog
    data = {
        "step_note": self.step_note_edit.text(),
        "step_disabled": self.step_disabled_chk.isChecked(),
        "editor_hidden": self.editor_hidden_chk.isChecked(),
        "jump_only": self.jump_only_chk.isChecked(),
        "custom_en": self.enable_chk.isChecked(),
        "custom_conf": self.conf_edit.text(),
        "custom_scale_min": self.s_min_edit.text(),
        "custom_scale_max": self.s_max_edit.text(),
        "custom_scale_step": self.s_step_edit.text(),
        "custom_gray": self.gray_chk.isChecked(),
        "inverse_match_en": self.inverse_match_chk.isChecked(),
        "image_confirm_click_en": self.image_confirm_click_chk.isChecked(),
        "image_confirm_click_delay": self.image_confirm_click_delay_edit.text(),
        "template_mask_regions": list(self.template_mask_regions),
        "delayed_second_click_en": self.delayed_second_click_chk.isChecked(),
        "delayed_second_click_delay": self.delayed_second_click_delay_edit.text(),
        "multi_image_actions": self.multi_image_action_editor.rules(),
        "repeat_mode": self.repeat_combo.currentText(),
        "repeat_count": self.repeat_count_edit.text(),
        "step_loop_start": self.step_loop_start_edit.text(),
        "step_loop_end": self.step_loop_end_edit.text(),
        "point_limit_en": self.point_limit_chk.isChecked(),
        "point_limit_count": self.point_limit_count_edit.text(),
        "image_click_point_en": self.image_click_point_chk.isChecked(),
        "image_click_point_rx": self.image_click_point_rx,
        "image_click_point_ry": self.image_click_point_ry,
        "step_region_en": self.step_region_chk.isChecked(),
        "step_region": self.step_region_edit.text().strip(),
        "capture_region_mode": self.capture_region_mode_combo.currentData()
        or "global",
        "capture_region": self.capture_region_edit.text().strip(),
        "coord_step_en": self.coord_step_chk.isChecked(),
        "coord_step_every": self.coord_step_every_edit.text(),
        "coord_step_direction": self.coord_step_direction_combo.currentText(),
        "coord_step_distance": self.coord_step_distance_edit.text(),
        "coord_step_dx": self.coord_step_dx_edit.text(),
        "coord_step_dy": self.coord_step_dy_edit.text(),
        "coord_step_point": self.coord_step_point_edit.text(),
        "coord_step_max_steps": self.coord_step_max_steps_edit.text(),
        "coord_step_max_distance": self.coord_step_max_distance_edit.text(),
        "coord_step_stop": self.coord_step_stop_chk.isChecked(),
        "coord_step_reset_after": self.coord_step_reset_after_edit.text(),
        "coord_step_manual_points": serialize_coord_step_manual_points(
            self.coord_step_manual_points
        ),
        "coord_sequence_en": self.coord_sequence_chk.isChecked(),
        "coord_sequence_points": serialize_coordinate_sequence(
            self.coord_sequence_points()
        ),
        "coord_sequence_end_action": self.coord_sequence_end_combo.currentText(),
        "coord_sequence_batch_mode": self.coord_sequence_batch_combo.currentData()
        or "one",
        "coord_sequence_batch_count": self.coord_sequence_batch_count_edit.text(),
        "background_click_en": self.background_click_chk.isChecked(),
        "background_click_binding": dict(self.background_click_binding),
        "fail_limit": self.fail_limit_edit.text(),
        "no_skip_wait": self.no_skip_wait_chk.isChecked(),
        "run_max_executions": self.run_max_executions_edit.text(),
        "debug_breakpoint": self.debug_breakpoint_chk.isChecked(),
        "debug_condition": self.debug_condition_edit.text().strip(),
        "success_skip": self.success_skip_edit.text(),
        "success_jump": self.success_jump_edit.text(),
        "fail_skip": self.fail_skip_edit.text(),
        "fail_jump": self.fail_jump_edit.text(),
        "call_step": self.call_step_edit.text(),
        "drag_source_img": self.drag_source_img_edit.text().strip(),
        "drag_source_point": self.drag_source_point_edit.text().strip(),
        "drag_target_img": self.drag_target_img_edit.text().strip(),
        "drag_target_point": self.drag_target_point_edit.text().strip(),
    }
    data.update(
        {
            "until_logic": self.until_logic_combo.currentText(),
            "until_false_jump": self.until_false_jump_edit.text(),
            "until_true_jump": self.until_true_jump_edit.text(),
            "until_max_checks": self.until_max_checks_edit.text(),
            "until_max_seconds": self.until_max_seconds_edit.text(),
            "until_on_limit": self.until_on_limit_combo.currentText(),
        }
    )
    # 步骤级随机延迟仅在用户填写时写入。留空则不写入该键，引擎
    # _parse_jitter_value 收不到值返回 None，回退到全局移动/按住随机延迟，
    # 避免空值被解析为显式 0.0 而静默覆盖全局设置。
    move_jitter_text = self.move_jitter_edit.text().strip()
    if move_jitter_text:
        data["move_jitter"] = move_jitter_text
    click_hld_jitter_text = self.click_hld_jitter_edit.text().strip()
    if click_hld_jitter_text:
        data["click_hld_jitter"] = click_hld_jitter_text
    # 本步点击形态：点击偏移 / 移动曲线 / 点击后返回 / 系统按键按住时长。
    # 与前两项步骤级随机延迟保持一致——仅当用户填写时才写入，留空回退全局，
    # 避免空字符串被解析为显式 0 而静默覆盖全局设置。
    click_spatial_text = self.click_spatial_jitter_edit.text().strip()
    if click_spatial_text:
        data["click_spatial_jitter"] = click_spatial_text
    move_curve_text = self.move_curve_strength_edit.text().strip()
    if move_curve_text:
        data["move_curve_strength"] = move_curve_text
    data["mouse_trail_mode"] = (
        self.mouse_trail_mode_combo.currentData() or "inherit"
    )
    # 点击后返回为布尔开关，按勾选状态写入；关闭即 False，引擎忽略对非点击命令。
    data["click_return"] = self.click_return_chk.isChecked()
    key_hold_text = self.key_hold_duration_edit.text().strip()
    if key_hold_text:
        data["key_hold_duration"] = key_hold_text
    # 连点器（原生指令 28.0）参数：始终写入（UI 有默认值），引擎按字段解析，
    # 空值回退默认值。控件值优先取 currentData（按键/中心模式），文本取 .text()。
    data["ac_button"] = self.ac_button_combo.currentData() or "left"
    data["ac_center_mode"] = self.ac_center_mode_combo.currentData() or "current"
    data["ac_click_count"] = self.ac_click_count_edit.text().strip() or "1"
    data["ac_big_loop_count"] = self.ac_big_loop_count_edit.text().strip() or "1"
    data["ac_interval_min"] = self.ac_interval_min_edit.text().strip() or "0"
    data["ac_interval_max"] = self.ac_interval_max_edit.text().strip() or "0"
    data["ac_press_duration"] = self.ac_press_duration_edit.text().strip() or "0"
    data["ac_big_loop_interval"] = (
        self.ac_big_loop_interval_edit.text().strip() or "0"
    )
    data["ac_fixed_x"] = self.ac_fixed_x_edit.text().strip() or "0"
    data["ac_fixed_y"] = self.ac_fixed_y_edit.text().strip() or "0"
    data["ac_jitter_h"] = self.ac_jitter_h_edit.text().strip() or "0"
    data["ac_jitter_v"] = self.ac_jitter_v_edit.text().strip() or "0"
    for cond_idx, widgets in self.until_condition_widgets.items():
        data.update(
            {
                f"until_cond{cond_idx}_en": widgets["enabled"].isChecked(),
                f"until_cond{cond_idx}_mode": widgets["mode"].currentText(),
                f"until_cond{cond_idx}_image": widgets["image"].text().strip(),
                f"until_cond{cond_idx}_region": widgets["region"].text().strip(),
                f"until_cond{cond_idx}_sub_region": widgets["sub_region"]
                .text()
                .strip(),
                f"until_cond{cond_idx}_conf": widgets["conf"].text().strip(),
                f"until_cond{cond_idx}_diff": widgets["diff"].text().strip(),
                f"until_cond{cond_idx}_similarity": widgets["similarity"]
                .text()
                .strip(),
                f"until_cond{cond_idx}_overlay_image": widgets["overlay_image"]
                .text()
                .strip(),
                f"until_cond{cond_idx}_overlay_rect": widgets["overlay_rect"]
                .text()
                .strip(),
                f"until_cond{cond_idx}_overlay_scale": widgets["overlay_scale"]
                .text()
                .strip(),
                f"until_cond{cond_idx}_overlay_threshold": widgets[
                    "overlay_threshold"
                ]
                .text()
                .strip(),
                f"until_cond{cond_idx}_stable_seconds": widgets["stable"]
                .text()
                .strip(),
            }
        )
    # 声音提示（14.0）每步覆盖：仅在该步骤类型下写入，优先级高于全局设置。
    # 清空音频则回退全局默认；播放时长留空回退默认 30 秒。其它步骤不携带这些
    # 字段，避免污染无关数据。
    if command_code(self.step_type) == TASK_TYPE_SOUND_ALERT:
        data["sound_alert_audio_path"] = (
            self.sound_alert_audio_path_edit.text().strip()
        )
        data["sound_alert_play_seconds"] = (
            self.sound_alert_play_seconds_edit.text().strip() or "30"
        )
    return data
