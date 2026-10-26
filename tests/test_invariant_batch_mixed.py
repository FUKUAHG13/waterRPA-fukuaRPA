"""Regression tests for invariant #82: batch mixed values.

The coordinate-sequence batch feature (coord_sequence_batch_mode /
coord_sequence_batch_count) must accept MIXED task parameter values in a
single batch and select/execute them all without type errors:

  - coordinate points may be written in mixed textual formats (with/without
    spaces) and still parse into a uniform, executable batch;
  - the batch parameters (coord_sequence_batch_mode / coord_sequence_batch_count)
    may be supplied as mixed Python types (str / int / float) and still be
    honoured;
  - batch_mode "all" returns the entire batch so every point is executed.

The engine's batch-selection logic is exercised directly (headless, no Qt,
no real mouse movement).
"""

import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine
from fukua_rpa.task_model import parse_coordinate_sequence


class BatchMixedValuesInvariantTests(unittest.TestCase):
    def create_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        return engine

    def _coord_task(self, points, batch_mode, batch_count):
        return {
            "type": 0.0,
            "value": "100,200",  # a plain coordinate click command
            "coord_sequence_en": True,
            "coord_sequence_points": points,
            "coord_sequence_end_action": "点完后跳过本步",
            "coord_sequence_batch_mode": batch_mode,
            "coord_sequence_batch_count": batch_count,
        }

    def test_parse_coordinate_sequence_handles_mixed_formats(self):
        # Whitespace-only variation around integer coordinates; all must parse.
        points = parse_coordinate_sequence("10,20;  30 , 40 ; 50, 60 ;70 ,80")
        self.assertEqual(points, [(10, 20), (30, 40), (50, 60), (70, 80)])

    def test_batch_all_executes_every_mixed_point(self):
        engine = self.create_engine()
        points = "100,200;  300 , 400 ; 500 ,600 ; 700,800"
        task = self._coord_task(points, "all", "1")
        options = engine.coord_sequence_options(task)
        self.assertIsNotNone(options)
        locations, _state, status = engine._coord_sequence_locations(
            {"step": 1}, options
        )
        # batch_mode "all" selects the whole batch -> every point is executed.
        self.assertEqual(status, "ok")
        self.assertEqual(len(locations), 4)
        for point in locations:
            self.assertEqual(len(point), 2)
            self.assertIsInstance(point[0], int)
            self.assertIsInstance(point[1], int)

    def test_mixed_batch_count_types_no_type_errors(self):
        engine = self.create_engine()
        points = "10,20; 30,40; 50,60; 70,80; 90,100"
        step_info = {"step": 1}
        for count in ("2", 2, 2.0):  # str / int / float all accepted
            task = self._coord_task(points, "count", count)
            options = engine.coord_sequence_options(task)
            self.assertIsNotNone(options)
            locations, _state, status = engine._coord_sequence_locations(
                step_info, options
            )
            self.assertEqual(status, "ok")
            self.assertEqual(len(locations), 2)

    def test_batch_one_selects_single_point(self):
        engine = self.create_engine()
        points = "10,20; 30,40; 50,60"
        task = self._coord_task(points, "one", "1")
        options = engine.coord_sequence_options(task)
        locations, _state, status = engine._coord_sequence_locations(
            {"step": 1}, options
        )
        self.assertEqual(status, "ok")
        self.assertEqual(len(locations), 1)

    def test_mixed_batch_mode_strings_accepted(self):
        engine = self.create_engine()
        points = "10,20; 30,40; 50,60"
        for mode in ("one", "count", "all"):
            task = self._coord_task(points, mode, "1")
            options = engine.coord_sequence_options(task)
            self.assertIsNotNone(options)
            self.assertEqual(options["batch_mode"], mode)


if __name__ == "__main__":
    unittest.main()
