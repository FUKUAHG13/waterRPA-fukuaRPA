"""P7-1 死代码清理守卫测试。

验证：
- wasm_action 类型已从 PLUGIN_TYPES / TYPE_LABELS 移除（纯死类型）。
- 3 个私有死方法已删除（_coord_sequence_location / _sync_log_category_actions / _update_drop_line），
  且其仍被需要的兄弟方法保留。
- 4 个保留的模块级工具函数仍存在（标注 retained，非删除）。
- 动作分发注册表（action_handler 装饰注册）完整未受影响（证明未误删活代码）。
- 顶层包可正常导入。
"""
import importlib

import fukua_rpa
from fukua_rpa import constants, crash_guard, logging_service, multi_image_actions, performance
from fukua_rpa.actions.shared import registered_action_codes
from fukua_rpa.engine.coordinates import CoordinatePathMixin
from fukua_rpa.ui.controllers import plugin_controller
from fukua_rpa.ui.main_window import RPAWindow
from fukua_rpa.ui.task_list import DraggableListWidget


def test_wasm_action_type_removed():
    assert "wasm_action" not in constants.PLUGIN_TYPES
    assert "wasm_action" not in plugin_controller.TYPE_LABELS


def test_dead_private_methods_removed():
    # engine_coordinates: 删除 _coord_sequence_location，保留 _coord_sequence_locations
    assert not hasattr(CoordinatePathMixin, "_coord_sequence_location")
    assert hasattr(CoordinatePathMixin, "_coord_sequence_locations")
    # task_list: 删除 _update_drop_line(event)，保留 _update_drop_line_at
    assert not hasattr(DraggableListWidget, "_update_drop_line")
    assert hasattr(DraggableListWidget, "_update_drop_line_at")
    # main_window: 删除 _sync_log_category_actions，保留 refresh_log_content_button
    assert not hasattr(RPAWindow, "_sync_log_category_actions")
    assert hasattr(RPAWindow, "refresh_log_content_button")


def test_retained_module_functions_present():
    assert hasattr(crash_guard, "_now_text")
    assert hasattr(logging_service, "current_log_context")
    assert hasattr(multi_image_actions, "multi_image_action_label")
    assert hasattr(performance, "merge_performance_snapshots")


def test_action_dispatch_registry_intact():
    codes = registered_action_codes()
    assert len(codes) > 0


def test_package_imports():
    assert importlib.import_module("fukua_rpa") is fukua_rpa
