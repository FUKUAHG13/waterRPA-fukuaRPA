"""从 MappingController 提取的职责域 Mixin（God Class 拆分）。

按键映射的行管理、目标窗口绑定与热键录入方法组搬移至此包，
控制器仅保留宿主引用、backend 所有权与点击分发。Mixin 不持有
任何状态，跨 Mixin 调用经 MRO 解析。
"""

from .binding import MappingBindingMixin
from .hotkey_capture import MappingHotkeyCaptureMixin
from .row_management import MappingRowMixin

__all__ = ["MappingBindingMixin", "MappingHotkeyCaptureMixin", "MappingRowMixin"]
