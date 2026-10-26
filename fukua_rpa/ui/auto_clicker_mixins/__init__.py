"""从 AutoClickerToolWindow 提取的职责域 Mixin（God Class 拆分）。

连点器工具窗口的运行生命周期、全局快捷键与高级选项交互方法组搬移至
此包，窗口仅保留构造、布局构建与少量事件路由。Mixin 不持有任何状态，
跨 Mixin 调用经 MRO 解析。
"""

from .hotkey import HotkeyMixin
from .lifecycle import RunnerLifecycleMixin
from .options import ClickerOptionsMixin

__all__ = ["ClickerOptionsMixin", "HotkeyMixin", "RunnerLifecycleMixin"]
