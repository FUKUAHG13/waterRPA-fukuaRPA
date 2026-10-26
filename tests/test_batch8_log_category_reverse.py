"""Reverse guards for A5: category filtering must never lose a real error.

A5 (maintainer ruling 2026-10-07) mandated the *order*: build the reverse
verification first, prove it, and only then migrate call sites. These tests are
that verification, kept as permanent regression guards.

They are deliberately **reverse** tests. The forward question — "does an ordinary
application message get filtered when its category is off?" — only proves the new
filter works. The dangerous failure is the opposite one: a batch migration that
classifies a real error as an ordinary category would silently drop it while every
forward test still passed. So the invariant asserted here is the one that must
survive every future migration:

  with **every ordinary category disabled**, errors, emergency stops and severe
  warnings still reach the sink.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa import logging_service as ls  # noqa: E402
from fukua_rpa.log_policy import (  # noqa: E402
    LOG_APPLICATION,
    LOG_CRITICAL,
    LOG_STEP,
    LogPolicy,
)

# Real message shapes taken from actual call sites in this codebase. Text
# matching is not sufficient — none of these are in ``_CRITICAL_PATTERNS`` — which
# is exactly why severity is an explicit parameter instead of an inference.
ERROR_MESSAGES = (
    "脚本运行出错：第 3 步失败",
    "第 1 步【左键单击】执行失败：未找到图片",
    "急停：用户已中断运行",
    "严重警告：原生核心不可用，已回退到 OpenCV",
    "错误：配置文件损坏",
    "按键映射模式启动失败：注册失败",
    "忽略局部变化需要 OpenCV 掩膜识别，当前运行库不可用。",
)

ORDINARY_MESSAGES = (
    "已保存方案设置",
    "复制了 1 个步骤",
    "撤销一次编辑",
    "正在预加载资源...",
)


def _drain() -> list:
    """Empty the shared queue, balancing ``task_done`` per item."""
    taken = []
    while True:
        try:
            taken.append(ls.LOG_QUEUE.get_nowait())
        except Exception:
            break
        else:
            ls.LOG_QUEUE.task_done()
    return taken


class LogCategoryReverseTests(unittest.TestCase):
    def setUp(self):
        _drain()
        self._saved_policy = ls.GLOBAL_CONFIG.get("active_log_policy")
        self._saved_privacy = ls.GLOBAL_CONFIG.get("privacy_protected")
        ls.publish_log_policy(LogPolicy(mode="custom", custom_categories=()))
        self.addCleanup(self._restore)

    def _restore(self):
        ls.GLOBAL_CONFIG["active_log_policy"] = self._saved_policy
        ls.GLOBAL_CONFIG["privacy_protected"] = self._saved_privacy
        _drain()

    # --- the reverse invariant ------------------------------------------
    def test_critical_messages_survive_every_category_being_off(self):
        for message in ERROR_MESSAGES:
            with self.subTest(message=message):
                self.assertTrue(
                    ls.write_log(message, category=LOG_STEP, critical=True),
                    "严重消息不得被类别过滤丢弃",
                )

    def test_critical_messages_survive_under_any_category(self):
        """Whichever category a migration picks, an error must not depend on it."""
        from fukua_rpa.log_policy import LOG_CATEGORY_KEYS

        for key in LOG_CATEGORY_KEYS:
            if key == LOG_CRITICAL:
                continue
            with self.subTest(category=key):
                for message in ERROR_MESSAGES:
                    self.assertTrue(
                        ls.write_log(message, category=key, critical=True),
                        f"类别 {key} 下严重消息被丢弃：{message}",
                    )

    def test_critical_category_is_always_allowed_by_policy(self):
        policy = LogPolicy(mode="custom", custom_categories=())
        self.assertTrue(policy.allows(LOG_CRITICAL, critical=True, message="普通文本"))

    # --- the filter must actually filter (otherwise the above is vacuous)
    def test_ordinary_messages_are_filtered_when_their_category_is_off(self):
        for message in ORDINARY_MESSAGES:
            with self.subTest(message=message):
                self.assertFalse(
                    ls.write_log(message, category=LOG_APPLICATION),
                    "普通消息在类别关闭时必须被过滤",
                )

    def test_filtered_messages_are_not_queued(self):
        ls.write_log("已保存方案设置", category=LOG_APPLICATION)
        self.assertEqual(
            _drain(), [], "被过滤的消息不得留在队列里（过滤必须发生在入队前）"
        )

    def test_ordinary_messages_flow_when_their_category_is_enabled(self):
        ls.publish_log_policy(
            LogPolicy(mode="custom", custom_categories=(LOG_APPLICATION,))
        )
        for message in ORDINARY_MESSAGES:
            with self.subTest(message=message):
                self.assertTrue(ls.write_log(message, category=LOG_APPLICATION))

    # --- incremental-migration safety ----------------------------------
    def test_uncategorised_messages_keep_pre_a5_behaviour(self):
        """A not-yet-migrated call site must never be silently dropped."""
        for message in ORDINARY_MESSAGES:
            with self.subTest(message=message):
                self.assertTrue(
                    ls.write_log(message),
                    "未标注类别的调用必须仍然入队，否则渐进迁移会丢消息",
                )

    def test_absent_policy_allows_rather_than_drops(self):
        """``None`` means 'unknown here', which must never mean 'discard'."""
        ls.GLOBAL_CONFIG["active_log_policy"] = None
        self.assertTrue(ls.write_log("任意消息", category=LOG_APPLICATION))

    # --- category routing ----------------------------------------------
    def test_step_results_and_application_events_are_separated(self):
        ls.publish_log_policy(LogPolicy(mode="custom", custom_categories=(LOG_STEP,)))
        self.assertTrue(ls.write_log("步骤完成", category=LOG_STEP))
        _drain()
        self.assertFalse(
            ls.write_log("已保存方案设置", category=LOG_APPLICATION),
            "只启用步骤结果时，应用事件必须被过滤",
        )

    # --- privacy must survive the new filter --------------------------
    def test_privacy_still_strips_private_text_on_the_filtered_path(self):
        ls.GLOBAL_CONFIG["privacy_protected"] = True
        ls.write_log(
            "凭据 secret-value 不应出现在日志", category=LOG_STEP, critical=True
        )
        exposed = [r for r in _drain() if getattr(r, "private_message", None)]
        self.assertEqual(exposed, [], "隐私保护不得因新增过滤而被绕过")

    def test_diagnostics_and_crash_reports_stay_redacted(self):
        """A5: diagnostics and crash reports stay redacted, switch aside.

        Runs the real diagnostic probe with privacy **off**; the report must still
        declare that paths are redacted and must not leak the repo path.
        """
        from fukua_rpa.diagnostics import run_runtime_diagnostics

        ls.GLOBAL_CONFIG["privacy_protected"] = False
        report = run_runtime_diagnostics(base_dir=str(ROOT))
        self.assertTrue(report.get("private_paths_redacted"))
        self.assertNotIn(str(ROOT), str(report.get("checks")))


class QueueBackpressureTests(unittest.TestCase):
    """ADV-20261006-011: a full queue must never block the execution thread.

    A5's acceptance criteria keep this in scope: dropping is acceptable,
    blocking is not, and the counters must distinguish "queue full" from
    "sink raised" so a telemetry reader can tell a load problem from a bug.
    """

    def test_full_queue_drops_instead_of_blocking(self):
        import queue as queue_module

        drained = _drain()
        self.addCleanup(lambda: [ls.LOG_QUEUE.put_nowait(r) for r in drained])
        tiny = queue_module.Queue(maxsize=2)
        # Fill it so the next enqueue raises queue.Full immediately.
        tiny.put_nowait(object())
        tiny.put_nowait(object())
        started = time.monotonic()
        accepted = ls._enqueue_log_record(object(), target_queue=tiny)
        elapsed = time.monotonic() - started
        self.assertFalse(accepted, "满载时必须返回 False 而不是抛异常")
        self.assertLess(elapsed, 0.5, "满载路径不得阻塞调用线程")

    def test_drop_counters_distinguish_full_from_sink_failure(self):
        from fukua_rpa import logging_service as service

        snapshot = service.log_queue_stats()
        for key in ("dropped_total", "sink_failures", "capacity", "queued"):
            self.assertIn(key, snapshot, f"统计必须暴露 {key}")
        self.assertIsInstance(snapshot["dropped_total"], int)
        self.assertIsInstance(snapshot["sink_failures"], int)
        # Independence is proved by driving one counter and watching the other
        # stay put — a merged counter would move both.
        before_drops = snapshot["dropped_total"]
        before_failures = snapshot["sink_failures"]
        import queue as queue_module

        tiny = queue_module.Queue(maxsize=1)
        tiny.put_nowait(object())
        ls._enqueue_log_record(object(), target_queue=tiny)
        after = ls.log_queue_stats()
        self.assertEqual(after["dropped_total"], before_drops + 1, "满载必须计入丢弃计数")
        self.assertEqual(
            after["sink_failures"], before_failures, "满载不得被记成 sink 异常"
        )


if __name__ == "__main__":
    unittest.main()
