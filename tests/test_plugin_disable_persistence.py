"""Unittests for the persisted (machine-local) plugin disabled state.

Covers the maintainer ruling that plugin disabling uses **local persistent
semantics**:

* disabling survives an application restart and a plugin upgrade;
* the state is not exported with solutions (it lives outside plugin dirs);
* newly installed plugins default to disabled until the user enables them;
* uninstalling clears the persisted state for that plugin.

Run with ``python -m unittest -v tests.test_plugin_disable_persistence`` or via
``scripts/run_impacted_tests.py``. No PySide/Qt is imported.
"""

import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa.plugin_package import install_from_file
from fukua_rpa.plugin_registry import PluginRegistry, PluginStatus

from .test_plugin_manifest_package import (
    file_entries,
    valid_manifest,
    write_package,
)

PLUGIN_ID = "com.example.report"


class PluginDisablePersistenceTests(unittest.TestCase):
    """The disabled set must outlive the in-memory registry instance."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)
        self.contents = {"data.bin": b"x"}
        self._install(version="1.0.0")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _install(self, version="1.0.0"):
        """Install (or upgrade to) *version* from a freshly built package."""
        pkg = os.path.join(self.tmp, f"pkg-{version}.fkrplugin")
        write_package(
            pkg,
            valid_manifest(
                plugin_id=PLUGIN_ID,
                version=version,
                files=file_entries(self.contents),
            ),
            self.contents,
        )
        install_from_file(pkg, self.base)

    def _registry(self):
        return PluginRegistry(self.base, "1.0.0")

    def _persisted_ids(self):
        """Return the plugin ids recorded in the on-disk state file."""
        state_path = os.path.join(self.base, ".disabled_plugins.json")
        if not os.path.isfile(state_path):
            return set()
        import json

        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return set(data.get("disabled", []))

    # -- the four required semantics -------------------------------------

    def test_disabled_state_survives_registry_restart(self):
        """Disabling must persist across a new registry (i.e. a restart)."""
        first = self._registry()
        first.disable(PLUGIN_ID)
        self.assertEqual(first.status(PLUGIN_ID), PluginStatus.DISABLED)

        # A brand-new registry simulates restarting the application.
        second = self._registry()
        self.assertEqual(
            second.status(PLUGIN_ID),
            PluginStatus.DISABLED,
            "禁用状态必须在重启后保持（本机持久语义）",
        )

    def test_enabled_state_survives_upgrade(self):
        """An enabled plugin must stay enabled across an upgrade.

        升级走 install_from_file 的覆盖安装路径，不经 registry.uninstall()，
        故 _reviewed 保留、插件维持启用。若未来安装路径改为"先卸载再安装"，
        启用插件会被静默禁用并打断既有自动化——本测试用于锁定该语义。
        """
        registry = self._registry()
        registry.enable(PLUGIN_ID)
        self.assertEqual(registry.status(PLUGIN_ID), PluginStatus.ENABLED)

        self._install(version="2.0.0")

        upgraded = self._registry()
        self.assertEqual(
            upgraded.status(PLUGIN_ID),
            PluginStatus.ENABLED,
            "已启用插件升级后必须保持启用，否则既有自动化会被静默打断",
        )

    def test_disabled_state_survives_upgrade(self):
        """Upgrading a disabled plugin must keep it disabled."""
        registry = self._registry()
        registry.disable(PLUGIN_ID)

        # Upgrade installs a new version over the existing directory.
        self._install(version="2.0.0")

        upgraded = self._registry()
        self.assertEqual(
            upgraded.status(PLUGIN_ID),
            PluginStatus.DISABLED,
            "升级后必须保持禁用（本机持久语义）",
        )

    def test_newly_installed_plugin_is_disabled_by_default(self):
        """A plugin installed *after* initialisation stays disabled until enabled.

        setUp 安装的插件在首次运行时会被记为"存量已处理"，因此这里额外安装一个
        插件来复现真实的"新安装"路径。
        """
        bootstrap = self._registry()
        bootstrap.enable(PLUGIN_ID)

        second_id = "com.example.extra"
        pkg = os.path.join(self.tmp, "extra.fkrplugin")
        write_package(
            pkg,
            valid_manifest(
                plugin_id=second_id, files=file_entries(self.contents)
            ),
            self.contents,
        )
        install_from_file(pkg, self.base)

        registry = self._registry()
        self.assertEqual(
            registry.status(second_id),
            PluginStatus.DISABLED,
            "新安装插件默认禁用，用户检查后手动启用",
        )
        registry.enable(second_id)
        self.assertEqual(registry.status(second_id), PluginStatus.ENABLED)

    def test_uninstall_clears_persisted_state(self):
        """Uninstalling must drop that plugin's persisted disabled entry."""
        registry = self._registry()
        registry.disable(PLUGIN_ID)
        self.assertIn(PLUGIN_ID, self._persisted_ids())

        # 必须走注册表卸载，才能连同本机持久状态一起清理。
        removed, _ = registry.uninstall(PLUGIN_ID)
        self.assertTrue(removed)

        self.assertNotIn(
            PLUGIN_ID,
            self._persisted_ids(),
            "卸载时必须清理对应的持久状态",
        )

    # -- boundary: state must not travel with plugin content --------------

    def test_state_file_lives_outside_plugin_directories(self):
        """The state file must sit at the install root, not inside a plugin."""
        registry = self._registry()
        registry.disable(PLUGIN_ID)

        state_path = os.path.join(self.base, ".disabled_plugins.json")
        self.assertTrue(os.path.isfile(state_path))

        # Nothing persisted inside any plugin subdirectory, so packaging or
        # exporting a plugin directory can never carry the disabled state.
        for entry in os.scandir(self.base):
            if entry.is_dir():
                self.assertFalse(
                    os.path.exists(os.path.join(entry.path, ".disabled_plugins.json")),
                    "禁用状态不得写入插件目录内（否则会随插件/方案导出）",
                )

    def test_scan_ignores_the_state_file(self):
        """The dot-prefixed state file must never register as a ghost plugin."""
        registry = self._registry()
        registry.disable(PLUGIN_ID)

        ids = {record.plugin_id for record in registry.list_plugins()}
        self.assertEqual(ids, {PLUGIN_ID})


class ScanFailureMustNotDisableExistingPluginsTests(unittest.TestCase):
    """FR-SCOPE007-001 双档探针：扫描失败不得把存量插件永久变为禁用。

    缺陷链：``__init__`` 用 ``contextlib.suppress(OSError)`` 吞掉 ``scan()``
    失败 ⇒ ``_records`` 为空 ⇒ ``_load_disabled_state()`` 走"无状态文件"分支，
    把 **空集** 写入 ``reviewed`` 并落盘 ⇒ 下次启动读到空 ``reviewed``，全部
    存量插件被当作新安装而默认禁用，且**永不自愈**（状态文件已存在，不会再走
    首次运行分支）。

    边界：新安装插件**仍须默认禁用**；``_disabled`` 的既有语义不变。
    """

    PLUGIN_COUNT = 4

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)
        self.contents = {"data.bin": b"x"}
        self.ids = [f"com.example.p{index}" for index in range(self.PLUGIN_COUNT)]
        for plugin_id in self.ids:
            pkg = os.path.join(self.tmp, f"{plugin_id}.fkrplugin")
            write_package(
                pkg,
                valid_manifest(
                    plugin_id=plugin_id,
                    version="1.0.0",
                    files=file_entries(self.contents),
                ),
                self.contents,
            )
            install_from_file(pkg, self.base)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def state_path(self):
        return os.path.join(self.base, ".disabled_plugins.json")

    def _enabled_ids(self):
        registry = PluginRegistry(self.base, "1.0.0")
        return {
            plugin_id
            for plugin_id in self.ids
            if registry.is_enabled(plugin_id)
        }

    def test_control_group_two_normal_starts_keep_every_plugin_enabled(self):
        """对照组：两次正常构造后 4/4 保持启用。"""

        self.assertFalse(os.path.isfile(self.state_path), "前置：尚无状态文件")
        self.assertEqual(self._enabled_ids(), set(self.ids))
        self.assertEqual(self._enabled_ids(), set(self.ids))

    def test_scan_failure_does_not_disable_existing_plugins(self):
        """故障组：首次扫描失败后，存量插件仍须保持启用。"""

        import fukua_rpa.plugin_registry as plugin_registry

        self.assertFalse(os.path.isfile(self.state_path), "前置：尚无状态文件")
        with mock.patch.object(
            plugin_registry.os, "scandir", side_effect=OSError("目录暂时不可访问")
        ):
            failed = PluginRegistry(self.base, "1.0.0")
        # 扫描失败的这一次构造里没有登记任何插件。
        self.assertEqual(failed.list_plugins(), [])

        self.assertEqual(
            self._enabled_ids(),
            set(self.ids),
            "扫描失败后的下一次启动必须仍把存量插件视为已处理（4/4 启用）",
        )

    def test_scan_failure_does_not_write_the_state_file(self):
        """扫描失败不得落盘"空 reviewed"——否则首次运行信号被永久消耗。"""

        import fukua_rpa.plugin_registry as plugin_registry

        with mock.patch.object(
            plugin_registry.os, "scandir", side_effect=OSError("目录暂时不可访问")
        ):
            PluginRegistry(self.base, "1.0.0")

        self.assertFalse(
            os.path.isfile(self.state_path),
            "扫描失败时不得写入状态文件",
        )

    def test_newly_installed_plugin_is_still_disabled_after_a_failed_scan(self):
        """边界：修复不得破坏"新安装插件默认禁用"。"""

        import fukua_rpa.plugin_registry as plugin_registry

        with mock.patch.object(
            plugin_registry.os, "scandir", side_effect=OSError("目录暂时不可访问")
        ):
            PluginRegistry(self.base, "1.0.0")

        # 首次成功启动把存量 4 个插件记为已处理。
        self.assertEqual(self._enabled_ids(), set(self.ids))

        newcomer = "com.example.newcomer"
        pkg = os.path.join(self.tmp, "newcomer.fkrplugin")
        write_package(
            pkg,
            valid_manifest(
                plugin_id=newcomer, files=file_entries(self.contents)
            ),
            self.contents,
        )
        install_from_file(pkg, self.base)

        registry = PluginRegistry(self.base, "1.0.0")
        self.assertEqual(
            registry.status(newcomer),
            PluginStatus.DISABLED,
            "新安装插件仍须默认禁用",
        )
        self.assertEqual(registry.status(self.ids[0]), PluginStatus.ENABLED)

    def test_same_instance_failed_then_successful_scan_keeps_existing_plugins(self):
        """FR-20260930-006：同一实例内先扫描失败、后成功扫描，不得把存量插件判为
        未审核（此前失败时连状态**读取**也被跳过，_reviewed 为空，后续成功 scan()
        后 _status() 对每个插件返回 DISABLED）。"""

        import fukua_rpa.plugin_registry as plugin_registry

        self.assertFalse(os.path.isfile(self.state_path), "前置：尚无状态文件")
        with mock.patch.object(
            plugin_registry.os, "scandir", side_effect=OSError("目录暂时不可访问")
        ):
            registry = PluginRegistry(self.base, "1.0.0")
        self.assertEqual(registry.list_plugins(), [], "失败构造不登记任何插件")

        # 同一实例目录恢复后再扫成功：存量插件必须保持启用。
        registry.scan()
        self.assertEqual(
            {plugin_id for plugin_id in self.ids if registry.is_enabled(plugin_id)},
            set(self.ids),
            "同一实例失败→成功扫描后，存量插件不得被判为未审核",
        )


class MalformedPluginStateFileTests(unittest.TestCase):
    """FR-20261005-601: 畸形状态文件必须降级为空状态，绝不能阻断启动。

    ``_load_disabled_state`` 的 docstring 明文要求"损坏或超限的状态文件按空状态
    处理（失败关闭）"。修复前字段为非容器（int/bool）时集合推导抛 TypeError 冒泡出
    ``PluginRegistry.__init__``，使应用无法启动；字符串字段还会被逐字符迭代。
    """

    def _registry(self, payload):
        import json
        import shutil
        import tempfile

        from fukua_rpa.plugin_registry import _DISABLED_STATE_NAME, PluginRegistry

        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        with open(os.path.join(temp_dir, _DISABLED_STATE_NAME), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        return PluginRegistry(temp_dir, "v1.0.0")

    def test_non_container_fields_do_not_raise(self):
        for payload in (
            {"disabled": 5},
            {"reviewed": True},
            {"disabled": 0, "reviewed": 0},
            {"disabled": None, "reviewed": None},
        ):
            registry = self._registry(payload)
            self.assertEqual(registry._disabled, set())
            self.assertEqual(registry._reviewed, set())

    def test_string_field_is_not_iterated_character_by_character(self):
        registry = self._registry({"disabled": "abc"})
        self.assertEqual(registry._disabled, set())

    def test_valid_list_is_still_loaded(self):
        registry = self._registry({"disabled": ["plugin-a", 7], "reviewed": ["plugin-b"]})
        self.assertEqual(registry._disabled, {"plugin-a"})
        self.assertEqual(registry._reviewed, {"plugin-b"})
