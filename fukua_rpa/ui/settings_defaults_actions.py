# pyright: reportAttributeAccessIssue=false
"""Default-settings template actions (2026-10-21 maintainer request).

Two user-facing actions live in the settings header row:

- 将当前设置设定为默认设置: snapshot the current global settings as the
  template used by **newly created profiles**.
- 恢复默认设置: reset the current profile's **global settings** back to the
  factory defaults.

Both are deliberately scoped to global settings only: the step list, the
profile name and the (now shared) key-mapping set are never touched.
"""

import copy
import json

from PySide6.QtWidgets import QMessageBox

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import MAX_PROFILE_STORE_JSON_BYTES
from fukua_rpa.json_safety import loads_bounded_json
from fukua_rpa.log_policy import LOG_APPLICATION

DEFAULT_TEMPLATE_KEY = "default_profile_template_json"

#: 不进入模板的字段：步骤列表属于方案内容；映射字段已全局共享（见 config_store）。
_TEMPLATE_EXCLUDED = ("tasks",)
_MAPPING_FIELDS = (
    "key_mappings",
    "key_mapping_count",
    "mapping_mode_enabled",
    "mapping_click_mode",
)


def _template_payload(config):
    """Return the global-settings subset that may seed a new profile."""
    return {
        key: copy.deepcopy(value)
        for key, value in (config or {}).items()
        if key not in _TEMPLATE_EXCLUDED and key not in _MAPPING_FIELDS
    }


def load_default_template(settings):
    """Return the stored template dict, or None when absent/invalid.

    SAFETY: 模板存放在用户可写的配置里，解析走统一的有界入口（ARCHITECTURE #61 /
    `docs/设计基准.md` 的 JSON 解析边界），不再使用裸 ``json.loads``（FR-20261021-005）。
    """
    if settings is None:
        return None
    raw = settings.value(DEFAULT_TEMPLATE_KEY, "")
    if not raw:
        return None
    try:
        data = loads_bounded_json(
            raw,
            max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
            max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
            label="默认设置模板",
        )
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def save_default_template(settings, config):
    """Persist the current settings as the template for new profiles."""
    if settings is None:
        return
    settings.setValue(
        DEFAULT_TEMPLATE_KEY, json.dumps(_template_payload(config), ensure_ascii=False)
    )
    sync = getattr(settings, "sync", None)
    if callable(sync):
        sync()


def build_new_profile_config(host):
    """Return the config used when the user creates a new profile.

    The stored template wins; missing keys fall back to factory defaults so a
    template written by an older version still yields a complete profile.
    The shared key-mapping set is carried over from the current profile.
    """
    base = default_profile_config()
    template = load_default_template(getattr(host, "settings", None))
    if template:
        base.update(copy.deepcopy(template))
    current = {}
    try:
        current = host.get_current_ui_config() or {}
    except Exception:  # noqa: BLE001 - 读不到当前配置时退回默认映射
        current = {}
    for field in _MAPPING_FIELDS:
        if field in current:
            base[field] = copy.deepcopy(current[field])
    base["tasks"] = []
    return base


def set_current_settings_as_default(host):
    """Snapshot current global settings as the default for new profiles."""
    config = host.get_current_ui_config() or {}
    save_default_template(getattr(host, "settings", None), config)
    QMessageBox.information(
        host,
        "已设为默认设置",
        "当前方案的各项设置已保存为默认设置。\n\n"
        "以后新建方案会沿用这些设置；步骤列表、方案名与按键映射不受影响。",
    )
    log = getattr(host, "append_application_log", None)
    if callable(log):
        log("已把当前设置设为默认设置。", category=LOG_APPLICATION)


def restore_factory_settings(host):
    """Reset the current profile's global settings to factory defaults."""
    answer = QMessageBox.question(
        host,
        "恢复默认设置",
        "将当前方案的各项设置恢复为软件初始默认值？\n\n"
        "步骤列表、方案名与按键映射不会被清除；设置项本身不进入撤销栈，"
        "恢复后需重新调整。",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if answer != QMessageBox.Yes:
        return
    current = host.get_current_ui_config() or {}
    factory = default_profile_config()
    # 保留方案内容与全局映射；其余设置项回到出厂值。
    factory["tasks"] = copy.deepcopy(current.get("tasks", []))
    for field in _MAPPING_FIELDS:
        if field in current:
            factory[field] = copy.deepcopy(current[field])
    host.apply_ui_config(factory)
    controller = getattr(host, "profile_controller", None)
    mark_dirty = getattr(controller, "mark_dirty", None)
    if callable(mark_dirty):
        mark_dirty(force=True)
    log = getattr(host, "append_application_log", None)
    if callable(log):
        log("已把当前设置恢复为默认设置。", category=LOG_APPLICATION)
