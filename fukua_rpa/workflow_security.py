"""Static workflow capability review and privacy-preserving export helpers."""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .commands import COMMAND_BY_CODE, IMAGE_TARGET_COMMANDS
from .config_schema import default_profile_config, migrate_profile_config
from .constants import (
    TASK_TYPE_ACTIVATE_WINDOW,
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_CLOSE_WINDOW,
    TASK_TYPE_DIRECTORY_SELECT,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_INPUT_TIMELINE,
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_POPUP_NOTIFY,
    TASK_TYPE_SECRET_TEXT,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_SOUND_ALERT,
    TASK_TYPE_STOP_RUN,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_UIA_READ_VALUE,
    TASK_TYPE_UIA_SET_VALUE,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT_WINDOW,
)
from .multi_image_actions import MULTI_IMAGE_POINT_ACTIONS, normalize_multi_image_rules
from .profile_paths import MANAGED_DATA_PREFIX
from .task_images import iter_task_image_paths
from .task_model import TASK_DATA_DEFAULTS, config_bool, parse_coordinate_text

MOUSE_ACTIONS = frozenset(
    {1.0, 2.0, 3.0, 6.0, 8.0, 10.0, 11.0, TASK_TYPE_AUTO_CLICKER}
)
# 29.0 输入时序按 `input_events` 的实际内容分类（2026-10-21 裁定 ⑤）：
# 纯键盘只计键盘、纯鼠标只计鼠标、混合各计 1；步骤总数与危险清单仍只计一次。
# 因此它不在 MOUSE_ACTIONS 里，但**必须**显式保留在 DANGEROUS_ACTIONS。
KEYBOARD_ACTIONS = frozenset({4.0, 7.0, TASK_TYPE_SECRET_TEXT, TASK_TYPE_UIA_SET_VALUE})
WINDOW_ACTIONS = frozenset(
    {
        TASK_TYPE_LAUNCH_APP,
        TASK_TYPE_WAIT_WINDOW,
        TASK_TYPE_ACTIVATE_WINDOW,
        TASK_TYPE_CLOSE_WINDOW,
        TASK_TYPE_UIA_CLICK,
        TASK_TYPE_UIA_SET_VALUE,
        TASK_TYPE_UIA_READ_VALUE,
    }
)
DANGEROUS_ACTIONS = frozenset(
    {
        *MOUSE_ACTIONS,
        *KEYBOARD_ACTIONS,
        TASK_TYPE_INPUT_TIMELINE,  # 29.0：已移出 MOUSE_ACTIONS，必须显式保留
        9.0,
        12.0,
        13.0,
        14.0,
        TASK_TYPE_LAUNCH_APP,
        TASK_TYPE_ACTIVATE_WINDOW,
        TASK_TYPE_CLOSE_WINDOW,
        TASK_TYPE_UIA_CLICK,
        TASK_TYPE_UIA_SET_VALUE,
        TASK_TYPE_MULTI_IMAGE_ACTION,
        # REVIEW-20260913-001 FR-007: 插件动作是外部代码入口，按 ARCHITECTURE
        # #54 属"应允许用户一键停用后再导入"的高影响能力。此前它不在本集合、
        # 又因在 COMMAND_BY_CODE 内而不落入"未知动作"分支，导致能力摘要不统计、
        # 一键停用不覆盖。当前宿主仍是只校验的离线桩，故这不是已发生的高危
        # 安全问题，而是导入审查契约的完整性缺口。
        TASK_TYPE_PLUGIN_ACTION,
    }
)
KNOWN_PROFILE_FIELDS = frozenset(default_profile_config())
# Structural step fields that are not part of TASK_DATA_DEFAULTS but are
# legitimate keys in a serialized task dict.
#
# FR-20260829-②: 下面这批字段由 task_settings_binding 写入、actions/flow 消费
# （或被 bulk_task_settings 列为批量能力字段），属真实持久化配置，但从未进入
# TASK_DATA_DEFAULTS。它们在脱敏分享包中被当作"未知字段"剥离，并因剥离逻辑
# 把携带它们的步骤整体置 step_disabled=True，导致接收方看到的流程结构与发送方
# 不一致（审查 FR-007）。此处逐一登记为结构字段。
#
# 范围说明：除 sound_alert_audio_path 外，全部为非敏感的布尔/数值/枚举配置。
# sound_alert_audio_path 是本地绝对路径，属隐私内容：它必须留在本白名单内
# （否则会被当作未知字段整步剥离，导致接收方看不到步骤结构），但分享脱敏时
# 必须单独置空并停用该步骤——该处置在 sanitize_profile_for_sharing 的逐步骤
# 循环内实现，与图片步骤 value 同口径（REVIEW-20260913-001 FR-001 / DOC-001）。
_STRUCTURAL_TASK_FIELDS = frozenset({
    "type",
    "value",
    "retry",
    "window_timeout",
    # --- 鼠标动作 ---
    "click_return",
    "mouse_trail_mode",
    "move_curve_strength",
    "move_jitter",
    "click_spatial_jitter",
    "click_hld_jitter",
    # --- 键盘动作 ---
    "key_hold_duration",
    # --- 连点器（原生指令 28.0）---
    "ac_button",
    "ac_center_mode",
    "ac_click_count",
    "ac_interval_min",
    "ac_interval_max",
    "ac_interval_base",
    "ac_interval_variation",
    "ac_interval_concentration",
    "ac_interval_random_en",
    "ac_press_duration",
    "ac_fixed_x",
    "ac_fixed_y",
    "ac_jitter_h",
    "ac_jitter_v",
    "ac_jitter_shape",
    "ac_jitter_concentration",
    "ac_move_curve_strength",
    "ac_big_loop_count",
    "ac_big_loop_interval",
    # --- 声音提醒 ---
    "sound_alert_audio_path",
    "sound_alert_play_seconds",
})
# FR-20260809-D6-②: 插件动作字段（plugin_id/plugin_action_id/plugin_config/
# plugin_schema_version）刻意不在 KNOWN_TASK_FIELDS——脱敏分享时会被当作未知
# 字段移除，这是显式安全语义而非隐式行为：插件配置可能含秘密值，且目标环境
# 未必安装该插件。移除后步骤保留类型（27.0）但插件配置清空，接收方可识别为
# 被清理的插件动作。若未来需要保留非敏感插件元数据，须在此显式声明白名单并
# 逐一评估字段敏感度。
# Auto-derived from TASK_DATA_DEFAULTS to prevent drift.  Any new field added
# to the defaults dict is automatically recognized here.
KNOWN_TASK_FIELDS = frozenset(TASK_DATA_DEFAULTS.keys() | _STRUCTURAL_TASK_FIELDS)


@dataclass(frozen=True)
class WorkflowCapabilityReport:
    """Immutable static-analysis summary of a workflow's capabilities and risks.

    Produced by ``analyze_workflow_capabilities`` and shown to the user before
    an imported workflow is accepted, so they can see what the workflow is able
    to do (mouse/keyboard/window actions, image and credential references) and
    which steps would change the computer's state.

    Attributes:
        step_count: Total number of steps.
        enabled_step_count: Steps not marked disabled.
        disabled_step_count: Steps marked disabled.
        mouse_action_count: Steps performing mouse movement/click/drag.
        keyboard_action_count: Steps performing keyboard or text input.
        launch_count: Steps launching external programs.
        close_window_count: Steps closing windows.
        uia_action_count: Steps using background UI Automation.
        image_reference_count: Number of image references across all steps.
        missing_image_steps: 1-based indices of steps whose image is absent.
        credential_reference_count: Number of secret-text credential references.
        unknown_action_steps: 1-based indices of steps with an unknown action.
        dangerous_steps: 1-based indices of enabled steps that change state.
        enabled_mapping_count: Number of enabled key mappings.
        external_file_count: Steps referencing external files or programs.
    """

    step_count: int
    enabled_step_count: int
    disabled_step_count: int
    mouse_action_count: int
    keyboard_action_count: int
    launch_count: int
    close_window_count: int
    uia_action_count: int
    image_reference_count: int
    missing_image_steps: tuple[int, ...]
    credential_reference_count: int
    unknown_action_steps: tuple[int, ...]
    dangerous_steps: tuple[int, ...]
    enabled_mapping_count: int
    external_file_count: int

    @property
    def has_warnings(self) -> bool:
        """Return True when any finding warrants an explicit user warning."""
        return bool(
            self.dangerous_steps
            or self.enabled_mapping_count
            or self.missing_image_steps
            or self.unknown_action_steps
        )

    def summary_lines(self) -> tuple[str, ...]:
        """Return localized, human-readable lines summarizing the report."""
        lines = [
            f"步骤总数：{self.step_count}（启用 {self.enabled_step_count}，停用 {self.disabled_step_count}）",
            f"鼠标移动/点击/拖拽：{self.mouse_action_count} 步",
            f"键盘或文本输入：{self.keyboard_action_count} 步",
            f"启动程序：{self.launch_count} 步；关闭窗口：{self.close_window_count} 步",
            f"后台 UI Automation：{self.uia_action_count} 步",
            f"图片引用：{self.image_reference_count} 项；外部文件或程序引用：{self.external_file_count} 项",
            f"凭据名称引用：{self.credential_reference_count} 项（凭据值不在方案中）",
            f"已启用按键映射：{self.enabled_mapping_count} 项",
        ]
        if self.missing_image_steps:
            lines.append(
                "缺失图片所在步骤：" + "、".join(map(str, self.missing_image_steps))
            )
        if self.unknown_action_steps:
            lines.append(
                "未知动作所在步骤：" + "、".join(map(str, self.unknown_action_steps))
            )
        if self.dangerous_steps:
            preview = "、".join(map(str, self.dangerous_steps[:20]))
            suffix = "……" if len(self.dangerous_steps) > 20 else ""
            lines.append(f"会改变电脑状态的步骤：{preview}{suffix}")
        return tuple(lines)


@dataclass(frozen=True)
class PrivacySanitizationResult:
    """Outcome of redacting a profile for privacy-preserving sharing.

    Attributes:
        profile: The sanitized, detached profile dict safe to export.
        redacted_steps: 1-based indices of steps that were modified/disabled.
        removed_image_count: Number of image references stripped.
        disabled_mapping_count: Number of key mappings that were enabled and
            got disabled during sanitization.
    """

    profile: dict[str, Any]
    redacted_steps: tuple[int, ...]
    removed_image_count: int
    disabled_mapping_count: int


def classify_timeline_events(events) -> tuple[bool, bool, bool]:
    """Return ``(has_mouse, has_keyboard, has_unknown)`` for a 29.0 step.

    2026-10-21 裁定 ⑤：按事件实际内容统计能力。kind 取值**必须与播放器一致**
    （`actions/input_timeline.py` 只认 `key` / `mouse` / `move` / `wheel`），
    否则会出现「能力算上了、回放却丢弃」的错配（R17 审查 ④）。
    未知 kind 不参与分类，单独由 ``has_unknown`` 报告，调用方据此判为待核实。
    """
    has_mouse = False
    has_keyboard = False
    has_unknown = False
    for event in events if isinstance(events, (list, tuple)) else ():
        if not isinstance(event, Mapping):
            has_unknown = True
            continue
        kind = str(event.get("kind") or "").strip().lower()
        if kind == "key":
            has_keyboard = True
        elif kind in {"mouse", "move", "wheel"}:
            has_mouse = True
        else:
            has_unknown = True
    return has_mouse, has_keyboard, has_unknown


def _command_code(task: Mapping[str, Any]) -> float | None:
    try:
        return float(task.get("type", 0))
    except (TypeError, ValueError):
        return None


def _task_image_paths(task: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(iter_task_image_paths(task))


def _image_path_missing(path: str) -> bool:
    """Return True when an image path does not point to an existing file.

    Paths containing NUL bytes or otherwise rejected by the filesystem would
    make ``os.path.isfile`` raise; they are treated as missing (fail closed)
    instead, so a malformed workflow can never crash the capability review.

    ``fukua-data://`` managed references are resolved to real paths by
    ``resolve_profile_path`` only after the import archive is extracted, so
    ``os.path.isfile`` would always reject them; they are not treated as
    missing here.
    """

    if not path or "\x00" in path:
        return True
    if path.startswith(MANAGED_DATA_PREFIX):
        return False
    try:
        return not os.path.isfile(path)
    except (ValueError, OSError):
        return True


def analyze_workflow_capabilities(config: Mapping[str, Any]) -> WorkflowCapabilityReport:
    """Perform a read-only static review of a workflow's capabilities and risks.

    Counts mouse/keyboard/window/UIA actions, image and credential references,
    and flags enabled steps that would change the computer's state, unknown
    actions, and missing images. The input is migrated but never mutated.

    Args:
        config: Raw profile mapping to analyze.

    Returns:
        A ``WorkflowCapabilityReport`` summarizing the findings.
    """
    profile = migrate_profile_config(config).value
    tasks = profile.get("tasks", []) if isinstance(profile.get("tasks"), list) else []
    counters = {
        "enabled": 0,
        "disabled": 0,
        "mouse": 0,
        "keyboard": 0,
        "launch": 0,
        "close": 0,
        "uia": 0,
        "images": 0,
        "credentials": 0,
        "external": 0,
    }
    missing_images = []
    unknown = []
    dangerous = []
    for index, raw_task in enumerate(tasks, 1):
        task = raw_task if isinstance(raw_task, Mapping) else {}
        command = _command_code(task)
        disabled = config_bool(task.get("step_disabled", False))
        counters["disabled" if disabled else "enabled"] += 1
        if command not in COMMAND_BY_CODE:
            unknown.append(index)
        if command == TASK_TYPE_INPUT_TIMELINE:
            has_mouse, has_keyboard, has_unknown = classify_timeline_events(
                task.get("input_events")
            )
            if has_mouse:
                counters["mouse"] += 1
            if has_keyboard:
                counters["keyboard"] += 1
            if has_unknown or not (has_mouse or has_keyboard):
                # 空/非法/未知 kind 不判为安全，也不计入任何能力。
                unknown.append(index)
        elif command in MOUSE_ACTIONS:
            counters["mouse"] += 1
        elif command in KEYBOARD_ACTIONS:
            counters["keyboard"] += 1
        if command == TASK_TYPE_MULTI_IMAGE_ACTION:
            for rule in normalize_multi_image_rules(task.get("multi_image_actions", [])):
                action = str(rule.get("action", "") or "")
                if action in MULTI_IMAGE_POINT_ACTIONS:
                    counters["mouse"] += 1
                elif action in {"text", "hotkey"}:
                    counters["keyboard"] += 1
        if command == TASK_TYPE_LAUNCH_APP:
            counters["launch"] += 1
        if command == TASK_TYPE_CLOSE_WINDOW:
            counters["close"] += 1
        if command in (TASK_TYPE_UIA_CLICK, TASK_TYPE_UIA_SET_VALUE, TASK_TYPE_UIA_READ_VALUE):
            counters["uia"] += 1
        if command == TASK_TYPE_SECRET_TEXT:
            counters["credentials"] += 1
        paths = _task_image_paths(task)
        counters["images"] += len(paths)
        if paths and any(_image_path_missing(path) for path in paths):
            missing_images.append(index)
        if command in (9.0, TASK_TYPE_LAUNCH_APP) or paths:
            counters["external"] += 1
        if not disabled and (command in DANGEROUS_ACTIONS or command not in COMMAND_BY_CODE):
            dangerous.append(index)

    mappings = profile.get("key_mappings", [])
    enabled_mappings = sum(
        1
        for mapping in mappings if isinstance(mapping, Mapping) and config_bool(mapping.get("enabled", False))
    ) if isinstance(mappings, list) else 0
    return WorkflowCapabilityReport(
        step_count=len(tasks),
        enabled_step_count=counters["enabled"],
        disabled_step_count=counters["disabled"],
        mouse_action_count=counters["mouse"],
        keyboard_action_count=counters["keyboard"],
        launch_count=counters["launch"],
        close_window_count=counters["close"],
        uia_action_count=counters["uia"],
        image_reference_count=counters["images"],
        missing_image_steps=tuple(missing_images),
        credential_reference_count=counters["credentials"],
        unknown_action_steps=tuple(unknown),
        dangerous_steps=tuple(dangerous),
        enabled_mapping_count=enabled_mappings,
        external_file_count=counters["external"],
    )


def disable_dangerous_workflow_actions(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a migrated profile with all dangerous or unknown steps disabled.

    Steps whose action changes the computer's state (or is unknown) are marked
    disabled, every key mapping is turned off, and mapping mode is disabled, so
    an imported workflow can be opened for review without side effects. The
    input is not mutated; a migrated copy is returned.

    Args:
        config: Raw profile mapping to neutralize.

    Returns:
        A new profile dict with dangerous actions disabled.
    """
    profile = migrate_profile_config(config).value
    for raw_task in profile.get("tasks", []):
        if not isinstance(raw_task, dict):
            continue
        command = _command_code(raw_task)
        if command in DANGEROUS_ACTIONS or command not in COMMAND_BY_CODE:
            raw_task["step_disabled"] = True
    mappings = profile.get("key_mappings", [])
    if isinstance(mappings, list):
        for mapping in mappings:
            if isinstance(mapping, dict):
                mapping["enabled"] = False
    profile["mapping_mode_enabled"] = False
    return profile


def sanitize_profile_for_sharing(config: Mapping[str, Any]) -> PrivacySanitizationResult:
    """Produce a redacted profile safe for privacy-preserving sharing.

    This is the aggressive redaction used for shared exports: it drops unknown
    profile/task fields, clears sensitive values (including credential names),
    images, notes, UIA/background bindings and multi-image rules, disables every
    affected step, and removes all key mappings. The input is not mutated; a
    deep-copied sanitized profile is returned.

    Args:
        config: Raw profile mapping to sanitize.

    Returns:
        A ``PrivacySanitizationResult`` with the sanitized profile and counts.
    """
    profile = migrate_profile_config(config).value
    for key in tuple(profile):
        if key not in KNOWN_PROFILE_FIELDS:
            profile.pop(key, None)
    # FR-20260811-D5-002 / REVIEW-20260913-001 FR-001: 全局声音提醒音频路径是本机
    # 绝对路径（KNOWN_PROFILE_FIELDS 放行它导致分享包携带本机目录结构指纹），这里
    # 显式清空。步骤级同名字段在 _STRUCTURAL_TASK_FIELDS 内、因而属于
    # KNOWN_TASK_FIELDS，不会被下面的"未知字段"分支剥离，必须在逐步骤循环中
    # 单独置空（见该循环内的同名字段分支）。
    audio_path = str(profile.get("sound_alert_audio_path", "") or "").strip()
    if audio_path:
        profile["sound_alert_audio_path"] = ""
    redacted_steps = []
    removed_images = 0
    # FR-20260809-D6-②: 全量用命名常量/语义组表达，杜绝裸数字漏配——新增
    # 动作类型时只需维护常量或语义组。KEYBOARD_ACTIONS 已含 4.0/7.0
    # （按键映射/系统按键）与 22.0/24.0；其余类型逐一列出。
    # 29.0 输入时序同时携带键鼠事件，必须按敏感动作处理（脱敏分享须停用）。
    sensitive_value_actions = set(KEYBOARD_ACTIONS) | {
        TASK_TYPE_INPUT_TIMELINE,   # 29.0 输入时序
        TASK_TYPE_DIRECTORY_SELECT,  # 9.0 截图保存目录（value 为目录）
        TASK_TYPE_POPUP_NOTIFY,      # 12.0
        TASK_TYPE_STOP_RUN,          # 13.0
        TASK_TYPE_SOUND_ALERT,       # 14.0
        TASK_TYPE_UNTIL,             # 15.0
        TASK_TYPE_SET_VARIABLE,      # 16.0
        TASK_TYPE_EXPRESSION,        # 17.0
        TASK_TYPE_LAUNCH_APP,        # 18.0
        TASK_TYPE_WAIT_WINDOW,       # 19.0
        TASK_TYPE_ACTIVATE_WINDOW,   # 20.0
        TASK_TYPE_CLOSE_WINDOW,      # 21.0
        TASK_TYPE_UIA_CLICK,         # 23.0
        TASK_TYPE_UIA_READ_VALUE,    # 25.0
    }
    for index, task in enumerate(profile.get("tasks", []), 1):
        if not isinstance(task, dict):
            continue
        command = _command_code(task)
        redacted = False
        unknown_fields = set(task) - KNOWN_TASK_FIELDS
        for key in unknown_fields:
            task.pop(key, None)
        if unknown_fields:
            redacted = True
        if command not in COMMAND_BY_CODE:
            # S6: preserve the original step type when it is a numeric command
            # code so the recipient can still see what kind of step this was,
            # instead of forcing 0.0 and losing that information. Malformed /
            # non-numeric types (which are never valid commands) fall back to
            # 0.0. The step is disabled either way, so it never executes on
            # import regardless of its type.
            step_id = str(task.get("step_id", "") or "")
            original_type = task.get("type")
            preserved_type = original_type if isinstance(original_type, (int, float)) else 0.0
            task.clear()
            task.update(
                {
                    "type": preserved_type,
                    "value": "",
                    "step_id": step_id,
                    "step_disabled": True,
                }
            )
            redacted_steps.append(index)
            continue
        value = str(task.get("value", "") or "").strip()
        if command in IMAGE_TARGET_COMMANDS and value and not parse_coordinate_text(value):
            task["value"] = ""
            removed_images += 1
            redacted = True
        if command in sensitive_value_actions:
            if value:
                task["value"] = ""
                redacted = True
            # 29.0 输入时序的事件载荷与 value 分离存放：只清 value 会把按键/坐标
            # 时序留在脱敏包里（审查 D1 复验发现）。此处一并清空并停用该步骤，
            # 且不依赖 value 是否为空——空 value 的时序步骤同样携带事件。
            if task.get("input_events"):
                task["input_events"] = []
                redacted = True
            if command == TASK_TYPE_INPUT_TIMELINE:
                # 时序元数据（总时长等）同样反映用户操作节奏，一并清除（R16 ⑥）。
                if task.get("recorded_duration") not in (None, "", 0):
                    task["recorded_duration"] = 0
                task["step_disabled"] = True
        # REVIEW-20260913-001 FR-001: 步骤级声音提醒音频路径是与全局字段同名的本机
        # 绝对路径，属隐私内容。它与图片步骤 value（456-459 行）同口径处置：置空并
        # 停用该步骤。字段本身保留在 KNOWN_TASK_FIELDS 内（避免整步剥离导致接收方
        # 看不到步骤结构，见 _STRUCTURAL_TASK_FIELDS 的范围说明）。
        if str(task.get("sound_alert_audio_path", "") or "").strip():
            task["sound_alert_audio_path"] = ""
            redacted = True
        if task.get("debug_condition"):
            task["debug_condition"] = ""
            redacted = True
        if task.get("step_note"):
            task["step_note"] = ""
            redacted = True
        if task.get("uia_binding"):
            task["uia_binding"] = {}
            redacted = True
        if task.get("background_click_binding"):
            task["background_click_binding"] = {}
            task["background_click_en"] = False
            redacted = True
        image_condition_removed = False
        for condition_index in range(1, 4):
            key = f"until_cond{condition_index}_image"
            if str(task.get(key, "") or "").strip():
                task[key] = ""
                removed_images += 1
                image_condition_removed = True
            # TST04（确认缺口）：覆盖匹配小图与主图同为条件引用模板图
            # （task_images.py 的共享枚举已一并纳入）；脱敏分享包必须同时清除
            # overlay 图字段，否则可能仍携带本机绝对路径引用（#46 意图偏差）。
            overlay_key = f"until_cond{condition_index}_overlay_image"
            if str(task.get(overlay_key, "") or "").strip():
                task[overlay_key] = ""
                removed_images += 1
                image_condition_removed = True
        if str(task.get("drag_target_img", "") or "").strip():
            task["drag_target_img"] = ""
            removed_images += 1
            image_condition_removed = True
        if str(task.get("drag_source_img", "") or "").strip():
            task["drag_source_img"] = ""
            removed_images += 1
            image_condition_removed = True
        if image_condition_removed:
            redacted = True
        rules = task.get("multi_image_actions", [])
        if isinstance(rules, list) and rules:
            removed_images += sum(
                1
                for rule in rules
                if isinstance(rule, Mapping)
                and str(rule.get("image", "") or "").strip()
            )
            task["multi_image_actions"] = []
            redacted = True
        if redacted:
            task["step_disabled"] = True
            redacted_steps.append(index)

    disabled_mappings = 0
    mappings = profile.get("key_mappings", [])
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            if config_bool(mapping.get("enabled", False)):
                disabled_mappings += 1
            mapping["enabled"] = False
            mapping["window_binding"] = {}
    profile["key_mappings"] = []
    profile["key_mapping_count"] = 0
    profile["mapping_mode_enabled"] = False
    if str(profile.get("mapping_click_mode", "")) == "后台窗口点击(实验)":
        profile["mapping_click_mode"] = "真实鼠标点击"
    return PrivacySanitizationResult(
        profile=copy.deepcopy(profile),
        redacted_steps=tuple(redacted_steps),
        removed_image_count=removed_images,
        disabled_mapping_count=disabled_mappings,
    )
