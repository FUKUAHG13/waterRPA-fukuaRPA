"""Coverage tests for fukua_rpa.scheduled_task_store — targets uncovered branches.

Covers: _validate_finite_seconds (non-dict task item, trigger sub-object
validation), _preserve_corrupt_file, ScheduledTaskStore.load() with corrupt
tasks/history files and malformed individual records, save() method, and
set_enabled() no-op on absent task.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fukua_rpa.scheduled_task_model import (
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTrigger,
    TriggerKind,
    new_schedule_id,
)
from fukua_rpa.scheduled_task_store import (
    ScheduledTaskStore,
    _preserve_corrupt_file,
    _validate_finite_seconds,
    safe_load_json,
)


def _make_task(name="t", enabled=True, profile_id="p1"):
    return ScheduledTask(
        schedule_id=new_schedule_id(),
        name=name,
        profile_id=profile_id,
        profile_name_snapshot="P1",
        enabled=enabled,
        trigger=ScheduledTrigger(kind=TriggerKind.DAILY, local_time=datetime.time(9, 0)),
    )


def _make_history(schedule_id="s1", history_id="h1"):
    return ScheduledRunHistory(
        history_id=history_id,
        schedule_id=schedule_id,
        run_id="r1",
        profile_id="p1",
        due_at_utc=datetime.datetime(2026, 7, 19, 9, 0),
        outcome="success",
    )


# ---------------------------------------------------------------------------
# _validate_finite_seconds — uncovered branches
# ---------------------------------------------------------------------------

class TestValidateFiniteSecondsExtra(unittest.TestCase):
    def test_non_dict_task_item_skipped(self):
        # Line 69: task item that is not a dict -> continue
        parsed = {"tasks": ["not_a_dict", 42, None, {"name": "ok"}]}
        # Should not raise
        _validate_finite_seconds(parsed)

    def test_trigger_interval_seconds_bool_rejected(self):
        # Lines 89-90: trigger.interval_seconds is bool -> ValueError
        parsed = {"tasks": [{"trigger": {"interval_seconds": True}}]}
        with self.assertRaises(ValueError) as ctx:
            _validate_finite_seconds(parsed)
        self.assertIn("trigger.interval_seconds", str(ctx.exception))

    def test_trigger_interval_seconds_string_rejected(self):
        parsed = {"tasks": [{"trigger": {"interval_seconds": "fast"}}]}
        with self.assertRaises(ValueError):
            _validate_finite_seconds(parsed)

    def test_trigger_startup_delay_bool_rejected(self):
        parsed = {"tasks": [{"trigger": {"startup_delay_seconds": False}}]}
        with self.assertRaises(ValueError) as ctx:
            _validate_finite_seconds(parsed)
        self.assertIn("trigger.startup_delay_seconds", str(ctx.exception))

    def test_trigger_interval_seconds_inf_rejected(self):
        # Lines 91-92: trigger.interval_seconds is inf -> ValueError
        parsed = {"tasks": [{"trigger": {"interval_seconds": float("inf")}}]}
        with self.assertRaises(ValueError) as ctx:
            _validate_finite_seconds(parsed)
        self.assertIn("有限数字", str(ctx.exception))

    def test_trigger_startup_delay_nan_rejected(self):
        parsed = {"tasks": [{"trigger": {"startup_delay_seconds": float("nan")}}]}
        with self.assertRaises(ValueError):
            _validate_finite_seconds(parsed)

    def test_trigger_none_values_skipped(self):
        # None values in trigger are allowed (sentinel for "not set")
        parsed = {"tasks": [{"trigger": {"interval_seconds": None, "startup_delay_seconds": None}}]}
        _validate_finite_seconds(parsed)

    def test_trigger_valid_numbers_pass(self):
        parsed = {"tasks": [{"trigger": {"interval_seconds": 60, "startup_delay_seconds": 5.5}}]}
        _validate_finite_seconds(parsed)


# ---------------------------------------------------------------------------
# _preserve_corrupt_file
# ---------------------------------------------------------------------------

class TestPreserveCorruptFile(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_renames_existing_file(self):
        path = os.path.join(self._tmp_dir, "tasks.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"tasks": []}')
        _preserve_corrupt_file(path)
        # Original should be gone
        self.assertFalse(os.path.exists(path))
        # A .corrupt.TIMESTAMP backup should exist
        backups = [f for f in os.listdir(self._tmp_dir) if ".corrupt." in f]
        self.assertEqual(len(backups), 1)

    def test_nonexistent_file_noop(self):
        path = os.path.join(self._tmp_dir, "nope.json")
        # Should not raise
        _preserve_corrupt_file(path)

    def test_oserror_suppressed(self):
        # If os.replace fails, the function should not raise
        path = os.path.join(self._tmp_dir, "locked.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        with patch("os.replace", side_effect=OSError("locked")):
            _preserve_corrupt_file(path)
        # Original still exists (replace failed)
        self.assertTrue(os.path.exists(path))


# ---------------------------------------------------------------------------
# ScheduledTaskStore.load() — corrupt file handling
# ---------------------------------------------------------------------------

class TestStoreLoadCorruptFiles(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self._tmp_dir, "scheduled_tasks.json")
        self.history_path = os.path.join(self._tmp_dir, "scheduled_task_history.json")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_corrupt_tasks_file_starts_empty(self):
        # Write invalid JSON to tasks file
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write("{invalid json!!!")
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(store.all(), [])
        # A .corrupt backup should have been created
        backups = [f for f in os.listdir(self._tmp_dir) if "scheduled_tasks.json.corrupt." in f]
        self.assertEqual(len(backups), 1)

    def test_corrupt_history_file_starts_empty(self):
        # Valid tasks file, corrupt history
        task = _make_task()
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": [task.to_dict()]}, f)
        with open(self.history_path, "w", encoding="utf-8") as f:
            f.write("not json at all")
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        # Tasks loaded fine
        self.assertEqual(len(store.all()), 1)
        # History empty
        self.assertEqual(store.all_history(), [])
        # History corrupt backup exists
        backups = [f for f in os.listdir(self._tmp_dir) if "scheduled_task_history.json.corrupt." in f]
        self.assertEqual(len(backups), 1)

    def test_malformed_task_record_skipped(self):
        # One valid task, one missing required keys
        valid = _make_task(name="good")
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": [valid.to_dict(), {"bad": "record"}]}, f)
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.all()[0].name, "good")

    def test_malformed_history_record_skipped(self):
        # One valid history, one missing required keys
        valid_h = _make_history()
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump({"history": [valid_h.to_dict(), {"oops": True}]}, f)
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(len(store.all_history()), 1)
        self.assertEqual(store.all_history()[0].history_id, "h1")

    def test_duplicate_key_tasks_file_treated_as_corrupt(self):
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write('{"tasks": [], "tasks": []}')
        store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(store.all(), [])

    def test_oversized_tasks_file_treated_as_corrupt(self):
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write("{}")
        # Patch MAX_TASKS_FILE_BYTES to 1 to trigger size check
        with patch("fukua_rpa.scheduled_task_store.MAX_TASKS_FILE_BYTES", 1):
            store = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(store.all(), [])


# ---------------------------------------------------------------------------
# ScheduledTaskStore.save() — explicit save method
# ---------------------------------------------------------------------------

class TestStoreSave(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self._tmp_dir, "scheduled_tasks.json")
        self.history_path = os.path.join(self._tmp_dir, "scheduled_task_history.json")
        self.store = ScheduledTaskStore(self.tasks_path, self.history_path)

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_save_writes_both_files(self):
        task = _make_task(name="saved")
        self.store.add(task)
        self.store.add_history(_make_history(schedule_id=task.schedule_id))
        # Delete files to prove save() recreates them
        os.remove(self.tasks_path)
        os.remove(self.history_path)
        self.store.save()
        self.assertTrue(os.path.exists(self.tasks_path))
        self.assertTrue(os.path.exists(self.history_path))
        # Reload and verify
        store2 = ScheduledTaskStore(self.tasks_path, self.history_path)
        self.assertEqual(len(store2.all()), 1)
        self.assertEqual(store2.all()[0].name, "saved")
        self.assertEqual(len(store2.all_history()), 1)

    def test_save_empty_store(self):
        self.store.save()
        with open(self.tasks_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["tasks"], [])
        with open(self.history_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["history"], [])


# ---------------------------------------------------------------------------
# set_enabled — no-op on absent task
# ---------------------------------------------------------------------------

class TestSetEnabledAbsent(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self._tmp_dir, "scheduled_tasks.json")
        self.history_path = os.path.join(self._tmp_dir, "scheduled_task_history.json")
        self.store = ScheduledTaskStore(self.tasks_path, self.history_path)

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_set_enabled_absent_task_noop(self):
        # Line 273: task is None -> return without error
        self.store.set_enabled("nonexistent-id", True)
        self.assertEqual(self.store.all(), [])

    def test_set_enabled_existing_task(self):
        task = _make_task(enabled=True)
        self.store.add(task)
        self.store.set_enabled(task.schedule_id, False)
        self.assertFalse(self.store.get(task.schedule_id).enabled)


# ---------------------------------------------------------------------------
# safe_load_json — additional edge cases via store integration
# ---------------------------------------------------------------------------

class TestSafeLoadJsonIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_trigger_finite_seconds_via_safe_load(self):
        # End-to-end: safe_load_json rejects trigger.interval_seconds = "bad"
        path = os.path.join(self._tmp_dir, "tasks.json")
        data = {"tasks": [{"trigger": {"interval_seconds": "bad"}}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with self.assertRaises(ValueError):
            safe_load_json(path)

    def test_valid_trigger_passes_safe_load(self):
        path = os.path.join(self._tmp_dir, "tasks.json")
        data = {"tasks": [{"trigger": {"interval_seconds": 30}}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        result = safe_load_json(path)
        self.assertEqual(result["tasks"][0]["trigger"]["interval_seconds"], 30)


if __name__ == "__main__":
    unittest.main()
