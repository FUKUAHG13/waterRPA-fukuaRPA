"""Bounded scheduled-log persistence and privacy-filtered sink tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from fukua_rpa.engine import RPAEngine
from fukua_rpa.log_policy import LOG_CRITICAL
from fukua_rpa.logging_service import (
    SCHEDULED_RUN_LOG_SOURCE,
    ProcessedLogEvent,
    build_run_log_context,
    flush_logs,
    register_processed_log_sink,
    unregister_processed_log_sink,
    write_log,
)
from fukua_rpa.scheduled_task_logs import ScheduledTaskLogStore


def _event(schedule_id="s1", run_id="r1", message="执行一步"):
    return ProcessedLogEvent(
        message=message,
        timestamp="2026-08-01 12:00:00.000",
        event_time_ns=1,
        context={
            "log_source": SCHEDULED_RUN_LOG_SOURCE,
            "schedule_id": schedule_id,
            "run_id": run_id,
        },
        structured={"event": "step", "step_index": 2, "secret": "ignored"},
    )


class TestScheduledTaskLogStore(unittest.TestCase):
    def test_run_context_requires_the_scheduled_source(self):
        self.assertEqual(
            build_run_log_context(
                {
                    "source": "profile_hotkey",
                    "schedule_id": "looks-like-a-schedule",
                    "run_id": "r1",
                }
            ),
            {},
        )
        self.assertEqual(
            build_run_log_context(
                {
                    "source": "scheduled",
                    "schedule_id": "s1",
                    "run_id": "r1",
                }
            ),
            {
                "log_source": SCHEDULED_RUN_LOG_SOURCE,
                "schedule_id": "s1",
                "run_id": "r1",
            },
        )

    def test_store_filters_by_schedule_and_run_and_keeps_safe_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScheduledTaskLogStore(os.path.join(tmp, "schedule.jsonl"))
            self.assertTrue(store.append_processed(_event("s1", "r1", "第一条")))
            self.assertTrue(store.append_processed(_event("s1", "r2", "第二条")))
            self.assertTrue(store.append_processed(_event("s2", "r1", "其他计划")))
            self.assertEqual(store.revision(), 3)

            rows = store.entries_for("s1", "r1")
            self.assertEqual([row["message"] for row in rows], ["第一条"])
            self.assertEqual(
                rows[0]["details"], {"event": "step", "step_index": 2}
            )
            self.assertNotIn("secret", rows[0]["details"])
            os.remove(store.path)
            self.assertEqual(store.entries_for("s1", "r1")[0]["message"], "第一条")

    def test_store_ignores_events_without_schedule_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schedule.jsonl")
            store = ScheduledTaskLogStore(path)
            event = ProcessedLogEvent("普通日志", "now", 1, {}, {})
            self.assertFalse(store.append_processed(event))
            self.assertFalse(os.path.exists(path))

    def test_store_rejects_unattributed_global_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schedule.jsonl")
            store = ScheduledTaskLogStore(path)
            event = ProcessedLogEvent(
                "设置界面事件",
                "now",
                1,
                {"schedule_id": "s1", "run_id": "r1"},
                {},
            )
            self.assertFalse(store.append_processed(event))
            self.assertFalse(os.path.exists(path))

    def test_long_ids_use_the_same_normalization_for_write_and_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScheduledTaskLogStore(os.path.join(tmp, "schedule.jsonl"))
            schedule_id = "s" * 300
            run_id = "r" * 300
            self.assertTrue(
                store.append_processed(_event(schedule_id, run_id, "超长 ID"))
            )
            self.assertEqual(
                [row["message"] for row in store.entries_for(schedule_id, run_id)],
                ["超长 ID"],
            )

    def test_long_ids_with_the_same_prefix_remain_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScheduledTaskLogStore(os.path.join(tmp, "schedule.jsonl"))
            common_schedule = "s" * 280
            common_run = "r" * 280
            schedule_a = common_schedule + "-one"
            schedule_b = common_schedule + "-two"
            run_a = common_run + "-one"
            run_b = common_run + "-two"
            self.assertTrue(store.append_processed(_event(schedule_a, run_a, "first")))
            self.assertTrue(store.append_processed(_event(schedule_b, run_b, "second")))

            self.assertEqual(
                [row["message"] for row in store.entries_for(schedule_a, run_a)],
                ["first"],
            )
            self.assertEqual(
                [row["message"] for row in store.entries_for(schedule_b, run_b)],
                ["second"],
            )

    def test_store_compacts_oldest_lines_and_clear_removes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "schedule.jsonl")
            store = ScheduledTaskLogStore(path, max_bytes=1024, compact_bytes=600)
            for index in range(30):
                self.assertTrue(
                    store.append_processed(
                        _event(message=f"{index:02d}-" + "x" * 120)
                    )
                )
            rows = store.entries_for("s1", "r1")
            self.assertTrue(rows)
            self.assertTrue(rows[-1]["message"].startswith("29-"))
            self.assertFalse(rows[0]["message"].startswith("00-"))
            self.assertLessEqual(os.path.getsize(path), 1024 + 512)

            store.clear()
            self.assertFalse(os.path.exists(path))

    def test_async_logger_delivers_processed_context_to_registered_sink(self):
        events: list[ProcessedLogEvent] = []
        sink = events.append
        register_processed_log_sink(sink)
        try:
            self.assertTrue(
                write_log(
                    "计划运行日志",
                    context={
                        "log_source": SCHEDULED_RUN_LOG_SOURCE,
                        "schedule_id": "s1",
                        "run_id": "r9",
                    },
                )
            )
            self.assertTrue(flush_logs(timeout=2.0))
        finally:
            unregister_processed_log_sink(sink)
        matching = [
            event for event in events if event.context.get("run_id") == "r9"
        ]
        self.assertTrue(matching)
        self.assertEqual(matching[-1].message, "计划运行日志")

    def test_in_process_engine_emits_explicit_scheduled_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScheduledTaskLogStore(os.path.join(tmp, "schedule.jsonl"))
            sink = store.append_processed
            register_processed_log_sink(sink)
            try:
                engine = RPAEngine(defer_backends=True)
                engine.run_log_context = build_run_log_context(
                    {
                        "source": "scheduled",
                        "schedule_id": "engine-schedule",
                        "run_id": "engine-run",
                    }
                )
                self.assertTrue(
                    engine.log(
                        "进程内计划日志",
                        LOG_CRITICAL,
                        critical=True,
                    )
                )
                self.assertTrue(flush_logs(timeout=2.0))
            finally:
                unregister_processed_log_sink(sink)
            self.assertEqual(
                [
                    row["message"]
                    for row in store.entries_for("engine-schedule", "engine-run")
                ],
                ["进程内计划日志"],
            )
