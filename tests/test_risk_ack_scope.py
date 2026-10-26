"""FR-20261005-001 回归守护：循环风险确认的去重键必须属于被检查的方案。

缺陷
----
``confirm_loop_risks`` 内部用 ``_current_profile_id()``（**当前编辑方案**）
作为去重键。多方案快捷启动走的是目标方案的独立快照且全程不切换
``current_name``（设计基准 §多方案快捷启动），于是：

1. 目标方案 A 的循环风险提示，可能被当前编辑方案 B 已确认过的同类型记录
   **静默短路**（该次热键启动不再提示）；
2. 用户确认后记录写进 B 的键，**污染** B 的去重集合——之后手动运行 B 时
   其自身提示可能被 A 的风险类型覆盖而跳过；同时 A 的确认从未落盘。

修复
----
``confirm_loop_risks`` 增加显式 ``profile_id`` 参数（缺省回退当前编辑方案，
保持手动运行路径不变），快捷键启动路径传入目标方案 id。

本文件在缺陷对象上因"显式 profile_id 参数不存在"而失败，在修复对象上通过。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox

from fukua_rpa.ui.controllers.workflow_review_controller import (
    WorkflowReviewController,
)


class _FakeSettings:
    """最小 QSettings 替身：记录写入值，模拟按 key 去重。"""

    def __init__(self):
        self._values = {}
        self.writes = []

    def value(self, key, default=None, type=None):  # noqa: A002 - 匹配 Qt 签名
        return self._values.get(key, default)

    def setValue(self, key, value):  # noqa: N802 - 匹配 Qt 签名
        self._values[key] = value
        self.writes.append((key, value))

    def sync(self):
        pass


class _FakeProfileModel:
    def __init__(self, current_name, ids):
        self.current_name = current_name
        self._ids = ids

    def profile_id_of(self, name):
        return self._ids.get(name)


class _FakeEngine:
    @staticmethod
    def get_cmd_name(_type):
        return "动作"


class _FakeHost:
    def __init__(self, current_name="B", ids=None):
        self.settings = _FakeSettings()
        self.profile_model = _FakeProfileModel(
            current_name, ids or {"A": "pid-A", "B": "pid-B"}
        )
        self.engine = _FakeEngine()


class _Controller(WorkflowReviewController):
    def __init__(self, host):
        self._host = host

    @property
    def host(self):
        return self._host

    def analyze_loop_risks(self, tasks, cfg):
        # 固定返回一条**能被 risk_type_keys 分类器识别**的风险文本，
        # 使去重逻辑（current_types <=已确认集合）真实生效，而不是因空类型
        # 集合而直接跳过提示。该文本对应 no_skip_wait 分类。
        return ["步骤 2 等待后使用【禁止跳过】标记"]


def _risky_tasks():
    # 含等待/循环语义，使 analyze_loop_risks 在真实实现下也会报风险；
    # 本测试用替身固定风险，tasks 内容只用于接口完整。
    return [{"type": 999, "action": "wait", "params": {"seconds": 1}}]


class _FakeButton:
    """QMessageBox.button 的最小替身，支持 setText。"""

    def setText(self, _text):
        pass


class _FakeMessageBox:
    """QMessageBox 的最小替身。

    直接构造 ``QMessageBox(parent)`` 需要真实的 QWidget 父对象，而本测试的
    host 是轻量替身。这里用一个纯 Python 类承接被测代码设置的属性与调用，
    并由 ``exec`` 返回预设结果，从而只验证去重键归属这一契约。
    """

    # 类级开关：``exec`` 的返回值（QMessageBox.Ok / Cancel）。
    result = QMessageBox.Ok
    # 类级计数器：实际显示提示框的次数。
    shown = 0
    # 类级记录：最后一次 setInformativeText 收到的正文。
    last_informative = ""

    def __init__(self, _parent=None):
        self._buttons = {}

    def setIcon(self, _icon):
        pass

    def setWindowTitle(self, _title):
        pass

    def setText(self, _text):
        pass

    def setInformativeText(self, text):
        type(self).last_informative = text

    def setDetailedText(self, _text):
        pass

    def setStandardButtons(self, _buttons):
        pass

    def button(self, which):
        self._buttons[which] = _FakeButton()
        return self._buttons[which]

    def exec(self):
        type(self).shown += 1
        return type(self).result


def _patched_box():
    """返回 (patch, restore) —— 把 QMessageBox 换成替身。"""
    original = QMessageBox

    class Patched(_FakeMessageBox):
        Ok = original.Ok
        Cancel = original.Cancel
        Warning = original.Warning

    return original, Patched


class RiskAckScopeTests(unittest.TestCase):
    """去重键必须是被检查方案，而非当前编辑方案。"""

    def setUp(self):
        self.host = _FakeHost(current_name="B")
        self.controller = _Controller(self.host)
        self.tasks = _risky_tasks()
        self.cfg = {"tasks": self.tasks}
        _FakeMessageBox.result = QMessageBox.Ok
        _FakeMessageBox.shown = 0
        _FakeMessageBox.last_informative = ""
        # 在被测模块中替换 QMessageBox 名称（被测代码以 QMessageBox(...) 构造）。
        import fukua_rpa.ui.controllers.workflow_review_controller as module

        self._module = module
        self._original_box = module.QMessageBox
        _, patched = _patched_box()
        module.QMessageBox = patched
        self._patched = patched

    def tearDown(self):
        self._module.QMessageBox = self._original_box

    def _confirm(self, profile_id):
        return self.controller.confirm_loop_risks(
            self.tasks, self.cfg, profile_id=profile_id
        )

    def test_accepts_explicit_profile_id_parameter(self):
        """核心守护：签名须支持显式传入被检查方案 id。

        缺陷对象上没有该参数，调用即 TypeError。
        """
        self.assertTrue(
            self._confirm("pid-A"),
            "确认风险后应返回 True（用户已确认继续）",
        )

    def test_ack_is_written_to_target_profile_key(self):
        """确认记录必须写入**目标方案**的键，而非当前编辑方案的键。"""
        self._confirm("pid-A")

        written_keys = [key for key, _ in self.host.settings.writes]
        self.assertTrue(
            any(key.startswith("risk_tip_acked_pid-A") for key in written_keys),
            f"确认记录应写入目标方案 pid-A 的键，实际写入：{written_keys}",
        )
        self.assertFalse(
            any(key.startswith("risk_tip_acked_pid-B") for key in written_keys),
            f"不得把目标方案（A）的确认记录写进当前编辑方案（B）的键，"
            f"实际写入：{written_keys}",
        )

    def test_target_profile_ack_does_not_suppress_target_prompt(self):
        """A 未确认而 B 已确认时，对 A 调用必须**仍然提示**。

        缺陷对象上会因沿用 B 的键而静默短路。
        """
        self.host.settings.setValue("risk_tip_acked_pid-B", "no_skip_wait")

        self._confirm("pid-A")

        self.assertEqual(
            self._patched.shown,
            1,
            "当前编辑方案（B）的确认记录不得静默短路目标方案（A）的风险提示",
        )

    def test_same_profile_ack_does_suppress_repeat_prompt(self):
        """反向验收：同一方案已确认后不重复提示（去重仍然有效）。"""
        self.host.settings.setValue("risk_tip_acked_pid-A", "no_skip_wait")

        self._confirm("pid-A")

        self.assertEqual(
            self._patched.shown,
            0,
            "同一方案已确认过同类型风险后不应重复提示（去重逻辑不得被破坏）",
        )

    def test_default_none_falls_back_to_current_profile(self):
        """缺省调用（手动运行路径）必须回退到当前编辑方案，行为不变。"""
        self.host.settings.setValue("risk_tip_acked_pid-B", "no_skip_wait")

        self.controller.confirm_loop_risks(self.tasks, self.cfg)

        self.assertEqual(
            self._patched.shown,
            0,
            "缺省调用应沿用当前编辑方案的既有去重记录（手动运行行为不得改变）",
        )

    def test_prompt_exposes_permanent_disable_path(self):
        """反馈项：风险弹窗下方必须常显永久关闭入口说明。

        真实入口是“设置 → 高级模式 → 运行范围与流程 → 运行前风险提示”，
        文案必须与实际可关闭项一致，且继续运行/取消运行的行为不变。"""
        self.assertTrue(self._confirm("pid-A"))
        informative = self._patched.last_informative
        self.assertIn("如需永久关闭此提示", informative)
        self.assertIn("设置 → 高级模式 → 运行范围与流程", informative)
        self.assertIn("运行前风险提示", informative)



if __name__ == "__main__":
    unittest.main()
