"""从 TaskRow 提取的职责域 Mixin（God Class 拆分）。

任务步骤行控件的坐标/窗口/UIA 取点与按键捕获、快照/撤销/外部编辑提交、
分组/备注/标记呈现、配置对话框转发与能力查询方法组搬移至本包；
TaskRow 仅保留构造、鼠标事件路由、数据读写与少量状态同步。
Mixin 不持有任何状态，跨 Mixin 调用经 MRO 解析。
"""

from .config_capability import TaskRowConfigCapabilityMixin
from .edit_history import TaskRowEditHistoryMixin
from .picker import TaskRowPickerMixin
from .presentation import TaskRowPresentationMixin

__all__ = [
    "TaskRowConfigCapabilityMixin",
    "TaskRowEditHistoryMixin",
    "TaskRowPickerMixin",
    "TaskRowPresentationMixin",
]
