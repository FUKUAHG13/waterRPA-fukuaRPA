# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Scheduler/plugin helpers extracted verbatim from RPAWindow (main_window.py).

Each function takes the RPAWindow instance as its first argument and runs the
original method body unchanged; main_window.py delegates to it so the public
API is preserved.
"""

import logging
import os

from PySide6.QtWidgets import (
    QMenu,
    QPushButton,
)

from ..constants import APP_VERSION
from ..log_policy import (
    LOG_BACKEND,
)
from ..logging_service import write_log
from ..plugin_actions import set_plugin_registry
from ..plugin_host_client import PluginHostClient, set_plugin_host_client
from ..plugin_host_process import PluginHostProcess
from ..plugin_registry import PluginRegistry
from ..privacy import private_error_text, safe_error_name
from ..scheduled_task_service import ScheduledTaskService
from .controllers.plugin_controller import PluginController

logger = logging.getLogger(__name__)


def _plugin_usage_resolver(self, plugin_id: str) -> int:
    """Count how many steps across all profiles reference *plugin_id*."""
    count = 0
    for config in self.profile_model.profiles.values():
        for task in config.get("tasks", []):
            if isinstance(task, dict) and task.get("plugin_id") == plugin_id:
                count += 1
    return count



def _list_profiles_for_scheduler(self):
    """Return [(profile_id, name), ...] for the scheduler profile picker."""
    return [
        (self.profile_model.profile_id_of(name), name)
        for name in self.profile_model.profiles
    ]



def _plugin_enabled_status(self, plugin_id):
    if not plugin_id:
        return None
    try:
        return self.plugin_registry.is_enabled(plugin_id)
    except Exception as exc:
        logger.debug("检查插件启用状态失败: %s", exc, exc_info=True)
        return None



def init_scheduler_and_plugins(self):
    """Wire the scheduled-task service and plugin registry (called once)."""
    if getattr(self, "_scheduler_and_plugins_inited", False):
        return
    self._scheduler_and_plugins_inited = True

    self.plugin_install_dir = os.path.join(self.base_dir, "plugins")
    self.plugin_registry = PluginRegistry(self.plugin_install_dir, APP_VERSION)
    self.plugin_host_client = PluginHostClient(PluginHostProcess(self.plugin_registry))
    set_plugin_registry(self.plugin_registry)
    set_plugin_host_client(self.plugin_host_client)
    self.plugin_controller = PluginController(
        self.plugin_registry, self.plugin_install_dir, self._plugin_usage_resolver
    )

    self.scheduled_service = ScheduledTaskService(
        self.run_controller,
        self.execution_controller,
        self.base_dir,
        APP_VERSION,
        self,
    )
    self._scheduled_tasks_window = None
    self._plugin_center_window = None
    self._auto_clicker_window = None
    # 2026-10-19：按键映射工具窗口（映射配置编辑窗口，懒创建）。
    self._key_mapping_tool_window = None

    # Scan installed plugins and arm the scheduler once profiles are loaded.
    try:
        self.plugin_registry.scan()
    except Exception as error:  # never block startup on a plugin scan failure
        write_log(
            f"插件扫描失败: {safe_error_name(error)}",
            private_message="插件扫描失败: " + private_error_text(error),
                     category=LOG_BACKEND,
                     critical=True,
                 )
    # FR-b01-004: MISSED_ASK 错过策略必须真正询问用户。必须在 start() 之前连接，
    # 否则 start() 内的首次错过检测所发信号会无人监听、任务悬挂于 pending_ask。
    self.scheduled_service.missed_detected.connect(self._on_scheduled_missed_ask)
    self.scheduled_service.start()

    self._build_tools_menu()



def _build_tools_menu(self):
    tools_btn = QPushButton("工具")
    tools_btn.setProperty("variant", "ghost")
    tools_btn.setToolTip("计划任务、插件中心、连点器等工具")
    tools_menu = QMenu(tools_btn)
    sched_action = tools_menu.addAction("计划任务")
    sched_action.triggered.connect(self.show_scheduled_tasks_window)
    plugin_action = tools_menu.addAction("插件中心")
    plugin_action.triggered.connect(self.show_plugin_center_window)
    auto_clicker_action = tools_menu.addAction("连点器")
    auto_clicker_action.triggered.connect(self.show_auto_clicker_window)
    # 2026-10-19：按键映射从设置页迁到工具菜单（映射配置编辑窗口，关闭只隐藏）。
    key_mapping_action = tools_menu.addAction("按键映射")
    key_mapping_action.triggered.connect(self.show_key_mapping_tool_window)
    # 运行期只读门禁按控件名查找；映射编辑属设置入口，运行中必须禁用（FR-20261021-004）。
    self.key_mapping_action = key_mapping_action
    tools_btn.setMenu(tools_menu)
    self.tools_btn = tools_btn
    layout = self.settings_btn.parent().layout()
    layout.insertWidget(layout.indexOf(self.settings_btn), tools_btn)
