# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false

"""Thin bridge between the plugin registry and the plugin-center table view.

Owns the :class:`QAbstractTableModel` that lists installed plugins and a small
:class:`PluginController` that refreshes it when the registry changes. The model
only reflects registry state — it never mutates plugins itself; mutating calls
(install / enable / disable / uninstall) go through the controller's methods,
which delegate to :mod:`plugin_package` / :mod:`plugin_registry` and then refresh.

Design section 5.2 / 5.6: the table shows name, version, author, type, status,
capabilities, usage count, and last error, mirroring the plugin-center spec.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from ...plugin_registry import PluginRegistry, PluginStatus, PluginStatusRecord

logger = logging.getLogger(__name__)
# 第 0 列是视觉编号（#），不对应数据字段；其余列顺延一位。
COL_INDEX = 0
COL_NAME = 1
COL_VERSION = 2
COL_AUTHOR = 3
COL_TYPE = 4
COL_STATUS = 5
COL_CAPS = 6
COL_USAGE = 7
COL_ERROR = 8

COLUMN_COUNT = 9

HEADERS = [
    "#",
    "名称",
    "版本",
    "作者",
    "类型",
    "状态",
    "能力",
    "使用情况",
    "最近错误",
]

STATUS_LABELS = {
    PluginStatus.ENABLED: "已启用",
    PluginStatus.DISABLED: "已停用",
    PluginStatus.INCOMPATIBLE: "不兼容",
    PluginStatus.CORRUPT: "损坏",
    PluginStatus.DEPENDENCY_MISSING: "依赖缺失",
}

TYPE_LABELS = {
    "declarative": "模板插件",
    "python_action": "Python 动作插件",
}

ERROR_STATUSES = frozenset(
    {PluginStatus.INCOMPATIBLE, PluginStatus.CORRUPT, PluginStatus.DEPENDENCY_MISSING}
)


def format_status(record: PluginStatusRecord) -> str:
    return STATUS_LABELS.get(record.status, record.status.value)


def format_caps(record: PluginStatusRecord) -> str:
    parts = []
    if record.action_count:
        parts.append(f"{record.action_count} 动作")
    if record.template_count:
        parts.append(f"{record.template_count} 模板")
    if record.permission_count:
        parts.append(f"{record.permission_count} 权限")
    return " · ".join(parts) if parts else "—"


class PluginTableModel(QAbstractTableModel):
    """Read-only view over ``registry.list_plugins()`` with optional manual ordering.

    The optional manual order (a persisted list of plugin ids) only reorders the
    rows; it never mutates plugin data. Drag-and-drop reordering is enabled by the
    view through :meth:`moveRows`, which updates the order and emits
    :attr:`orderChanged` so the host can persist it.
    """

    orderChanged = Signal()

    def __init__(
        self,
        registry: PluginRegistry,
        usage_resolver: Callable[[str], int] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._registry = registry
        self._usage_resolver = usage_resolver
        self._usage_cache: dict[str, int] = {}  # FR-20260812 建议-性能-1
        self._all_rows: list[PluginStatusRecord] = []
        self._rows: list[PluginStatusRecord] = []
        self._order: list[str] = []
        self.refresh()

    # -- manual ordering --------------------------------------------------

    def set_order(self, ids: list[str] | None) -> None:
        """Apply a persisted visual order (list of plugin ids)."""
        self._order = list(ids) if ids else []
        if self._all_rows:
            self.beginResetModel()
            self._all_rows = self._apply_order(self._all_rows)
            self._rows = list(self._all_rows)
            self.endResetModel()

    def get_order(self) -> list[str]:
        """Current visual order of plugin ids (canonical set)."""
        return [r.plugin_id for r in self._all_rows]

    def _apply_order(self, records: list[PluginStatusRecord]) -> list[PluginStatusRecord]:
        if not self._order:
            return records
        by_id = {r.plugin_id: r for r in records}
        ordered = [by_id[i] for i in self._order if i in by_id]
        seen = set(self._order)
        ordered += [r for r in records if r.plugin_id not in seen]
        return ordered

    # -- refresh / filtering ----------------------------------------------

    def refresh(self) -> None:
        records = self._registry.list_plugins()
        # FR-20260812 建议-性能-1: 预计算 usage 缓存，避免每次渲染（data()
        # 每格调用）触发 O(profiles×tasks) 全量扫描。
        resolver = self._usage_resolver
        if resolver is not None:
            self._usage_cache = {}
            for record in records:
                try:
                    self._usage_cache[record.plugin_id] = int(
                        resolver(record.plugin_id) or 0
                    )
                except Exception as exc:
                    logger.debug("获取插件使用次数失败: %s", exc, exc_info=True)
                    self._usage_cache[record.plugin_id] = 0
        else:
            self._usage_cache = {}
        self.beginResetModel()
        self._all_rows = self._apply_order(list(records))
        self._rows = list(self._all_rows)
        self.endResetModel()

    def _usage(self, record: PluginStatusRecord) -> int:
        # FR-20260812 建议-性能-1: 优先读 refresh 时预计算的缓存。
        cache = getattr(self, "_usage_cache", None)
        if cache is not None:
            return int(cache.get(record.plugin_id, 0))
        if self._usage_resolver is None:
            return 0
        try:
            return int(self._usage_resolver(record.plugin_id) or 0)
        except Exception as exc:
            logger.debug("获取插件使用次数失败: %s", exc, exc_info=True)
            return 0

    def all_rows(self) -> list[PluginStatusRecord]:
        """Full unfiltered record set (canonical source for filtering)."""
        return self._all_rows

    def set_rows(self, rows: list[PluginStatusRecord]) -> None:
        """替换内部行数据（供筛选使用）。"""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    # -- drag / drop reorder ----------------------------------------------

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        return base

    def moveRows(
        self,
        sourceParent: QModelIndex,
        sourceRow: int,
        count: int,
        destParent: QModelIndex,
        destRow: int,
    ) -> bool:
        # SAFETY: manual reordering only makes sense on the unfiltered set.
        # When a filter is active the view disables dragging, but guard anyway
        # to avoid accidentally dropping plugins that are hidden by the filter.
        if sourceParent != destParent or sourceParent.isValid():
            return False
        if self._rows != self._all_rows:
            return False
        n = len(self._rows)
        if sourceRow < 0 or destRow < 0 or sourceRow >= n or destRow > n:
            return False
        if count <= 0 or sourceRow + count > n:
            return False
        self.beginMoveRows(
            sourceParent, sourceRow, sourceRow + count - 1, destParent, destRow
        )
        moved = self._rows[sourceRow : sourceRow + count]
        rest = self._rows[:sourceRow] + self._rows[sourceRow + count :]
        insert_at = destRow - count if destRow > sourceRow else destRow
        rest[insert_at:insert_at] = moved
        self._rows = rest
        self._all_rows = list(rest)
        self.endMoveRows()
        self._order = [r.plugin_id for r in self._rows]
        self.orderChanged.emit()
        return True

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else COLUMN_COUNT

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(HEADERS):
            return HEADERS[section]
        return None

    def record_at(self, index: QModelIndex) -> PluginStatusRecord | None:
        if not index.isValid():
            return None
        if 0 <= index.row() < len(self._rows):
            return self._rows[index.row()]
        return None

    def plugin_id_at(self, index: QModelIndex) -> str | None:
        rec = self.record_at(index)
        return rec.plugin_id if rec else None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        record = self.record_at(index)
        if record is None:
            return None
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_INDEX:
                return str(index.row() + 1)
            if col == COL_NAME:
                return record.name or record.plugin_id
            if col == COL_VERSION:
                api = f" (API {record.api_version})" if record.api_version else ""
                return f"{record.version}{api}"
            if col == COL_AUTHOR:
                return record.author or "—"
            if col == COL_TYPE:
                return TYPE_LABELS.get(record.plugin_type, record.plugin_type)
            if col == COL_STATUS:
                return format_status(record)
            if col == COL_CAPS:
                return format_caps(record)
            if col == COL_USAGE:
                return f"{self._usage(record)} 处引用"
            if col == COL_ERROR:
                if record.status == PluginStatus.CORRUPT and record.corrupt_reason:
                    return record.corrupt_reason
                return "—"
            return None

        if role == Qt.ForegroundRole:
            if col == COL_STATUS and record.status in ERROR_STATUSES:
                from PySide6.QtGui import QColor

                return QColor(0xC0, 0x39, 0x2B)  # red for error states
            return None

        if role == Qt.TextAlignmentRole:
            if col in (COL_INDEX, COL_STATUS, COL_USAGE):
                return Qt.AlignCenter
            return None

        return None


class PluginController:
    """Bridges the plugin table model and the registry.

    The host instantiates this once and hands :meth:`model` to the table view.
    Mutating operations refresh the model afterwards.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        install_base_dir: str,
        usage_resolver: Callable[[str], int] | None = None,
    ):
        self._registry = registry
        self._install_base_dir = install_base_dir
        self._usage_resolver = usage_resolver
        self._model = PluginTableModel(registry, usage_resolver)

    @property
    def registry(self) -> PluginRegistry:
        return self._registry

    def model(self) -> PluginTableModel:
        return self._model

    def refresh(self) -> None:
        self._model.refresh()

    def usage_count(self, plugin_id: str) -> int:
        """Return how many profile steps reference *plugin_id*."""
        if self._usage_resolver is None:
            return 0
        try:
            return int(self._usage_resolver(plugin_id) or 0)
        except Exception as exc:
            logger.debug("获取插件使用次数失败: %s", exc, exc_info=True)
            return 0

    def enable(self, plugin_id: str) -> None:
        self._registry.enable(plugin_id)
        self._model.refresh()

    def disable(self, plugin_id: str) -> None:
        self._registry.disable(plugin_id)
        self._model.refresh()

    def uninstall(self, plugin_id: str) -> tuple[bool, int]:
        result = self._registry.uninstall(plugin_id, resolver=self._usage_resolver)
        self._model.refresh()
        return result

    def rescan(self) -> None:
        self._registry.scan()
        self._model.refresh()

    def install_from_file(self, path: str):
        from ...plugin_package import install_from_file

        # SAFETY: 设计 §5.6 —— 运行中的插件不得被覆盖。安装侧复用注册表的活跃
        # 运行计数（不含方案引用，见 PluginRegistry.active_run_count），使覆盖
        # 更新在有进行中的运行时被拒绝，而不是把文件从运行中的实例下面换掉。
        # 解析器异常不吞：宁可安装大声失败，也不能静默绕过该守卫。
        manifest = install_from_file(
            path,
            self._install_base_dir,
            active_run_resolver=self._registry.active_run_count,
        )
        # SAFETY: install_from_file only writes files to disk; the in-memory
        # registry record for the new plugin does not exist until we re-scan,
        # otherwise the table would stay stale until a manual refresh. rescan()
        # both re-scans the directory and refreshes the model.
        self.rescan()
        return manifest
