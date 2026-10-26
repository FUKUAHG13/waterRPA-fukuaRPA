"""Coverage tests for fukua_rpa.engine.coordinates (CoordinatePathMixin).

Exercises coordinate-step and coordinate-sequence logic that is pure
computation, using a lightweight harness class that provides the helper
methods the mixin expects (as_bool, parse_float_value, parse_coordinate,
log, log_enabled, log_level, coord_step_states, coord_sequence_states).
"""

import unittest

from fukua_rpa.engine.coordinates import CoordinatePathMixin
from fukua_rpa.task_model import parse_coordinate_text, parse_float_text


class _Harness(CoordinatePathMixin):
    """Minimal host providing the interface CoordinatePathMixin expects."""

    def __init__(self, log_level=0):
        self.log_level = log_level
        self.coord_step_states = {}
        self.coord_sequence_states = {}
        self._logs = []

    def as_bool(self, value):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "启用", "是")
        return bool(value)

    def parse_float_value(self, value, default=0.0):
        return parse_float_text(value, default)

    def parse_coordinate(self, val):
        return parse_coordinate_text(val)

    def log(self, msg, category=None, *, critical=False, private_msg=None):
        self._logs.append(msg)

    def log_enabled(self, category=None, *, critical=False, message=""):
        return True


# ---------------------------------------------------------------------------
# coord_step_options
# ---------------------------------------------------------------------------

class TestCoordStepOptions(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_disabled_returns_none(self):
        self.assertIsNone(self.h.coord_step_options(None))
        self.assertIsNone(self.h.coord_step_options({}))
        self.assertIsNone(self.h.coord_step_options({"coord_step_en": False}))

    def test_enabled_basic(self):
        task = {"coord_step_en": True, "coord_step_every": 3, "coord_step_direction": "向上",
                "coord_step_distance": 10, "coord_step_dx": 1, "coord_step_dy": 2,
                "coord_step_point": "100,200", "coord_step_max_steps": 5,
                "coord_step_max_distance": 50.0, "coord_step_stop": True,
                "coord_step_reset_after": 8, "coord_step_manual_points": "{}"}
        opts = self.h.coord_step_options(task)
        self.assertIsNotNone(opts)
        self.assertEqual(opts["every"], 3)
        self.assertEqual(opts["direction"], "向上")
        self.assertEqual(opts["distance"], 10.0)
        self.assertEqual(opts["dx"], 1.0)
        self.assertEqual(opts["dy"], 2.0)
        self.assertEqual(opts["point"], "100,200")
        self.assertEqual(opts["max_steps"], 5)
        self.assertEqual(opts["max_distance"], 50.0)
        self.assertTrue(opts["stop"])
        self.assertEqual(opts["reset_after"], 8)

    def test_invalid_every_falls_back_to_one(self):
        task = {"coord_step_en": True, "coord_step_every": "abc"}
        opts = self.h.coord_step_options(task)
        self.assertEqual(opts["every"], 1)

    def test_nonfinite_every_falls_back_to_one(self):
        for raw in ("nan", "inf", "-inf", "1e309"):
            with self.subTest(raw=raw):
                opts = self.h.coord_step_options(
                    {"coord_step_en": True, "coord_step_every": raw}
                )
                self.assertEqual(opts["every"], 1)

    def test_every_minimum_is_one(self):
        task = {"coord_step_en": True, "coord_step_every": -5}
        opts = self.h.coord_step_options(task)
        self.assertEqual(opts["every"], 1)

    def test_string_en(self):
        task = {"coord_step_en": "true"}
        opts = self.h.coord_step_options(task)
        self.assertIsNotNone(opts)

    # -- FR-20261008-302：方向别名必须在运行期同样归一 -----------------------

    @staticmethod
    def _alias_for(canonical):
        from fukua_rpa.task_model import _DIRECTION_ALIASES

        return next(
            key for key, value in _DIRECTION_ALIASES.items() if value == canonical
        )

    def _runtime_positions(self, direction, point, max_steps=3, clicks=3):
        # 每次调用使用独立 harness：coord_step_states 按 step_info 缓存运行状态，
        # 复用同一个 harness 会让第二次调用从已推进的位置继续。
        harness = _Harness()
        task = {
            "coord_step_en": True,
            "coord_step_every": 1,
            "coord_step_direction": direction,
            "coord_step_distance": 0,
            "coord_step_dx": 0,
            "coord_step_dy": 0,
            "coord_step_point": point,
            "coord_step_max_steps": max_steps,
            "coord_step_max_distance": 0,
            "coord_step_stop": False,
            "coord_step_reset_after": 0,
            "coord_step_manual_points": "{}",
        }
        options = harness.coord_step_options(task)
        step_info = {"step": 1}
        _key, state = harness._get_coord_step_state(step_info, 0.0, 0.0)
        positions = [(state["x"], state["y"])]
        for _ in range(clicks):
            harness._advance_coord_step_state(state, options, step_info)
            positions.append((state["x"], state["y"]))
        return options["direction"], positions

    def test_mojibake_direction_alias_is_normalised_for_runtime(self):
        """FR-20261008-302：旧存档方向别名必须在运行期同样归一。

        别名是保留的迁移入口（DEC-20260930-002-04），校验与预览都归一，运行期此前
        没有，于是「移动到新点位」的别名落入线性分支（delta 恒为 0），点击停在起点。
        """
        alias = self._alias_for("移动到新点位")
        opts = self.h.coord_step_options(
            {"coord_step_en": True, "coord_step_direction": alias}
        )
        self.assertEqual(opts["direction"], "移动到新点位")

    def test_alias_runs_the_same_path_as_the_standard_direction(self):
        """FR-20261008-302：别名与标准方向的运行期位置序列必须一致。"""
        alias = self._alias_for("移动到新点位")
        standard_direction, standard = self._runtime_positions(
            "移动到新点位", "100,100"
        )
        aliased_direction, aliased = self._runtime_positions(alias, "100,100")
        self.assertEqual(standard_direction, "移动到新点位")
        self.assertEqual(aliased_direction, "移动到新点位")
        self.assertEqual(standard, aliased)
        # 必须真的沿路径推进，而不是原地不动。
        self.assertNotEqual(standard[0], standard[-1])


# ---------------------------------------------------------------------------
# coord_sequence_options
# ---------------------------------------------------------------------------

class TestCoordSequenceOptions(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_disabled_returns_none(self):
        self.assertIsNone(self.h.coord_sequence_options(None))
        self.assertIsNone(self.h.coord_sequence_options({}))
        self.assertIsNone(self.h.coord_sequence_options({"coord_sequence_en": False}))

    def test_no_points_returns_none(self):
        task = {"coord_sequence_en": True, "coord_sequence_points": ""}
        self.assertIsNone(self.h.coord_sequence_options(task))

    def test_valid_sequence(self):
        task = {
            "coord_sequence_en": True,
            "coord_sequence_points": "10,20;30,40;50,60",
            "coord_sequence_end_action": "点完后循环",
            "coord_sequence_batch_mode": "count",
            "coord_sequence_batch_count": 2,
        }
        opts = self.h.coord_sequence_options(task)
        self.assertIsNotNone(opts)
        self.assertEqual(opts["points"], [(10, 20), (30, 40), (50, 60)])
        self.assertEqual(opts["end_action"], "点完后循环")
        self.assertEqual(opts["batch_mode"], "count")
        self.assertEqual(opts["batch_count"], 2)

    def test_invalid_end_action_defaults(self):
        task = {
            "coord_sequence_en": True,
            "coord_sequence_points": "1,2",
            "coord_sequence_end_action": "无效动作",
        }
        opts = self.h.coord_sequence_options(task)
        self.assertEqual(opts["end_action"], "点完后跳过本步")

    def test_invalid_batch_mode_defaults(self):
        task = {
            "coord_sequence_en": True,
            "coord_sequence_points": "1,2",
            "coord_sequence_batch_mode": "invalid",
        }
        opts = self.h.coord_sequence_options(task)
        self.assertEqual(opts["batch_mode"], "one")

    def test_invalid_batch_count_defaults(self):
        task = {
            "coord_sequence_en": True,
            "coord_sequence_points": "1,2",
            "coord_sequence_batch_count": "xyz",
        }
        opts = self.h.coord_sequence_options(task)
        self.assertEqual(opts["batch_count"], 1)


# ---------------------------------------------------------------------------
# _coord_sequence_key
# ---------------------------------------------------------------------------

class TestCoordSequenceKey(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_with_step_info(self):
        self.assertEqual(self.h._coord_sequence_key({"step": 5}), 5)

    def test_with_none(self):
        self.assertEqual(self.h._coord_sequence_key(None), 0)

    def test_missing_step_key(self):
        self.assertEqual(self.h._coord_sequence_key({}), 0)


# ---------------------------------------------------------------------------
# _coord_sequence_locations
# ---------------------------------------------------------------------------

class TestCoordSequenceLocations(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_empty_points(self):
        locs, state, status = self.h._coord_sequence_locations({"step": 1}, {"points": []})
        self.assertEqual(locs, [])
        self.assertIsNone(state)
        self.assertEqual(status, "empty")

    def test_one_by_one(self):
        options = {"points": [(10, 20), (30, 40)], "batch_mode": "one",
                   "end_action": "点完后跳过本步"}
        locs, state, status = self.h._coord_sequence_locations({"step": 1}, options)
        self.assertEqual(status, "ok")
        self.assertEqual(locs, [(10, 20)])
        # Advance and get next
        self.h._advance_coord_sequence(state)
        locs2, state2, status2 = self.h._coord_sequence_locations({"step": 1}, options)
        self.assertEqual(locs2, [(30, 40)])
        # Advance past end -> done
        self.h._advance_coord_sequence(state2)
        locs3, _, status3 = self.h._coord_sequence_locations({"step": 1}, options)
        self.assertEqual(status3, "done")
        self.assertEqual(locs3, [])

    def test_batch_all(self):
        options = {"points": [(1, 1), (2, 2), (3, 3)], "batch_mode": "all",
                   "end_action": "点完后跳过本步"}
        locs, state, status = self.h._coord_sequence_locations({"step": 2}, options)
        self.assertEqual(status, "ok")
        self.assertEqual(locs, [(1, 1), (2, 2), (3, 3)])

    def test_batch_count(self):
        options = {"points": [(1, 1), (2, 2), (3, 3)], "batch_mode": "count",
                   "batch_count": 2, "end_action": "点完后跳过本步"}
        locs, state, status = self.h._coord_sequence_locations({"step": 3}, options)
        self.assertEqual(status, "ok")
        self.assertEqual(locs, [(1, 1), (2, 2)])

    def test_end_action_loop(self):
        options = {"points": [(10, 20)], "batch_mode": "one",
                   "end_action": "点完后循环"}
        locs, state, _ = self.h._coord_sequence_locations({"step": 4}, options)
        self.h._advance_coord_sequence(state)
        # Now index is past end; loop should reset to 0
        locs2, state2, status2 = self.h._coord_sequence_locations({"step": 4}, options)
        self.assertEqual(status2, "ok")
        self.assertEqual(locs2, [(10, 20)])

    def test_end_action_stay_last(self):
        options = {"points": [(10, 20), (30, 40)], "batch_mode": "one",
                   "end_action": "点完后停在最后一个"}
        locs, state, _ = self.h._coord_sequence_locations({"step": 5}, options)
        self.h._advance_coord_sequence(state)
        self.h._advance_coord_sequence(state)
        # Now past end; stay at last
        locs2, _, status2 = self.h._coord_sequence_locations({"step": 5}, options)
        self.assertEqual(status2, "ok")
        self.assertEqual(locs2, [(30, 40)])


# ---------------------------------------------------------------------------
# _advance_coord_sequence
# ---------------------------------------------------------------------------

class TestAdvanceCoordSequence(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_advance_increments_index(self):
        state = {"index": 0}
        self.h._advance_coord_sequence(state)
        self.assertEqual(state["index"], 1)

    def test_advance_none_is_noop(self):
        # 健壮性：传入 None 必须是安全 no-op。除「不抛异常」外，明确断言其返回值为
        # None（无副作用、无隐式状态变更），并与正常传参推进形成对照。
        state = {"index": 3}
        self.h._advance_coord_sequence(state)
        self.assertEqual(state["index"], 4)
        result = self.h._advance_coord_sequence(None)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _coord_step_key
# ---------------------------------------------------------------------------

class TestCoordStepKey(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_basic_key(self):
        key = self.h._coord_step_key({"step": 3}, 100.7, 200.9)
        self.assertEqual(key, (3, 100, 200))

    def test_none_step_info(self):
        key = self.h._coord_step_key(None, 50, 60)
        self.assertEqual(key, (0, 50, 60))


# ---------------------------------------------------------------------------
# _coord_step_delta
# ---------------------------------------------------------------------------

class TestCoordStepDelta(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_direction_down(self):
        dx, dy = self.h._coord_step_delta({"direction": "向下", "distance": 10.0, "dx": 0, "dy": 0})
        self.assertEqual((dx, dy), (0.0, 10.0))

    def test_direction_up(self):
        dx, dy = self.h._coord_step_delta({"direction": "向上", "distance": 5.0, "dx": 0, "dy": 0})
        self.assertEqual((dx, dy), (0.0, -5.0))

    def test_direction_left(self):
        dx, dy = self.h._coord_step_delta({"direction": "向左", "distance": 7.0, "dx": 0, "dy": 0})
        self.assertEqual((dx, dy), (-7.0, 0.0))

    def test_direction_right(self):
        dx, dy = self.h._coord_step_delta({"direction": "向右", "distance": 3.0, "dx": 0, "dy": 0})
        self.assertEqual((dx, dy), (3.0, 0.0))

    def test_custom_offset(self):
        dx, dy = self.h._coord_step_delta({"direction": "自定义偏移", "distance": 0, "dx": 4.0, "dy": -2.0})
        self.assertEqual((dx, dy), (4.0, -2.0))


# ---------------------------------------------------------------------------
# _get_coord_step_state / _reset_coord_step_state
# ---------------------------------------------------------------------------

class TestCoordStepStateManagement(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_get_creates_new_state(self):
        key, state = self.h._get_coord_step_state({"step": 1}, 100, 200)
        self.assertEqual(key, (1, 100, 200))
        self.assertEqual(state["base_x"], 100.0)
        self.assertEqual(state["base_y"], 200.0)
        self.assertEqual(state["x"], 100.0)
        self.assertEqual(state["y"], 200.0)
        self.assertEqual(state["clicks_since_move"], 0)
        self.assertEqual(state["offset_times"], 0)
        self.assertFalse(state["movement_locked"])

    def test_get_returns_existing_state(self):
        _, state1 = self.h._get_coord_step_state({"step": 1}, 100, 200)
        state1["x"] = 999.0
        _, state2 = self.h._get_coord_step_state({"step": 1}, 100, 200)
        self.assertEqual(state2["x"], 999.0)

    def test_reset_restores_base(self):
        _, state = self.h._get_coord_step_state({"step": 1}, 50, 60)
        state["x"] = 999.0
        state["y"] = 888.0
        state["clicks_since_move"] = 5
        state["clicks_since_reset"] = 10
        state["offset_times"] = 3
        state["movement_locked"] = True
        self.h._reset_coord_step_state(state)
        self.assertEqual(state["x"], 50.0)
        self.assertEqual(state["y"], 60.0)
        self.assertEqual(state["clicks_since_move"], 0)
        self.assertEqual(state["clicks_since_reset"], 0)
        self.assertEqual(state["offset_times"], 0)
        self.assertFalse(state["movement_locked"])


# ---------------------------------------------------------------------------
# _advance_coord_step_state
# ---------------------------------------------------------------------------

class TestAdvanceCoordStepState(unittest.TestCase):
    def setUp(self):
        self.h = _Harness(log_level=2)

    def _make_state(self, base_x=0, base_y=0):
        _, state = self.h._get_coord_step_state({"step": 1}, base_x, base_y)
        return state

    def _base_options(self, **overrides):
        opts = {
            "every": 2, "direction": "向下", "distance": 10.0,
            "dx": 0.0, "dy": 0.0, "point": "", "max_steps": 0,
            "max_distance": 0.0, "stop": False, "reset_after": 0,
            "manual_points": {},
        }
        opts.update(overrides)
        return opts

    def test_ok_when_not_yet_every(self):
        state = self._make_state()
        opts = self._base_options(every=3)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "ok")
        self.assertEqual(state["clicks_since_move"], 1)

    def test_moved_after_every_clicks(self):
        state = self._make_state()
        opts = self._base_options(every=2, direction="向下", distance=10.0)
        self.h._advance_coord_step_state(state, opts, {"step": 1})
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "moved")
        self.assertEqual(state["y"], 10.0)
        self.assertEqual(state["offset_times"], 1)

    def test_reset_after_triggers_reset(self):
        state = self._make_state(100, 200)
        opts = self._base_options(every=1, reset_after=2)
        self.h._advance_coord_step_state(state, opts, {"step": 1})
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "reset")
        self.assertEqual(state["x"], 100.0)
        self.assertEqual(state["y"], 200.0)

    def test_locked_stop_when_movement_locked_and_stop(self):
        state = self._make_state()
        state["movement_locked"] = True
        opts = self._base_options(stop=True)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "locked_stop")

    def test_locked_when_movement_locked_no_stop(self):
        state = self._make_state()
        state["movement_locked"] = True
        opts = self._base_options(stop=False)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "locked")

    def test_max_steps_locks(self):
        state = self._make_state()
        state["offset_times"] = 5
        opts = self._base_options(every=1, max_steps=5)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertIn(result, ("locked", "locked_stop"))
        self.assertTrue(state["movement_locked"])

    def test_max_distance_locks(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="向下", distance=100.0, max_distance=50.0)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertIn(result, ("locked", "locked_stop"))
        self.assertTrue(state["movement_locked"])

    def test_move_to_new_point_basic(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="移动到新点位", point="100,100", max_steps=2)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "moved")
        self.assertAlmostEqual(state["x"], 100.0)
        self.assertAlmostEqual(state["y"], 100.0)

    def test_move_to_new_point_invalid_format_locks(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="移动到新点位", point="invalid", max_steps=2, stop=True)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "locked_stop")
        self.assertTrue(state["movement_locked"])

    def test_move_to_new_point_reaches_target_locks(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="移动到新点位", point="100,100", max_steps=2)
        # First move: offset_times 0 -> 1
        self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(state["offset_times"], 1)
        # Second attempt: offset_times=1 >= max_offset_times=1 -> locked
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertIn(result, ("locked", "locked_stop"))

    def test_move_to_new_point_manual_override(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="移动到新点位", point="100,100",
                                  max_steps=3, manual_points={1: (50.0, 75.0)})
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "moved")
        self.assertAlmostEqual(state["x"], 50.0)
        self.assertAlmostEqual(state["y"], 75.0)

    def test_max_steps_with_stop_flag(self):
        state = self._make_state()
        state["offset_times"] = 3
        opts = self._base_options(every=1, max_steps=3, stop=True)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "locked_stop")

    def test_max_distance_with_stop_flag(self):
        state = self._make_state(0, 0)
        opts = self._base_options(every=1, direction="向右", distance=100.0,
                                  max_distance=50.0, stop=True)
        result = self.h._advance_coord_step_state(state, opts, {"step": 1})
        self.assertEqual(result, "locked_stop")


# ---------------------------------------------------------------------------
# coord_step_log_message
# ---------------------------------------------------------------------------

class TestCoordStepLogMessage(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_basic_direction_message(self):
        state = {"clicks_since_move": 1, "offset_times": 2, "clicks_since_reset": 0}
        options = {"every": 3, "direction": "向下", "reset_after": 0}
        msg = self.h.coord_step_log_message(100, 200, state, options)
        self.assertIn("100", msg)
        self.assertIn("200", msg)
        self.assertIn("第2次偏移", msg)
        self.assertIn("第2次后进行下次偏移", msg)

    def test_reset_text_included(self):
        state = {"clicks_since_move": 0, "offset_times": 0, "clicks_since_reset": 3}
        options = {"every": 2, "direction": "向下", "reset_after": 5}
        msg = self.h.coord_step_log_message(50, 60, state, options)
        self.assertIn("重置计数 4/5", msg)

    def test_move_to_point_message(self):
        state = {"clicks_since_move": 0, "offset_times": 1, "clicks_since_reset": 0}
        options = {"every": 2, "direction": "移动到新点位", "reset_after": 0,
                   "max_steps": 4, "manual_points": {}}
        msg = self.h.coord_step_log_message(75, 80, state, options)
        self.assertIn("第2/4个点位", msg)

    def test_move_to_point_manual_text(self):
        state = {"clicks_since_move": 0, "offset_times": 1, "clicks_since_reset": 0}
        options = {"every": 2, "direction": "移动到新点位", "reset_after": 0,
                   "max_steps": 4, "manual_points": {1: (10, 20)}}
        msg = self.h.coord_step_log_message(75, 80, state, options)
        self.assertIn("手动修正点", msg)

    def test_next_after_wraps_to_every(self):
        state = {"clicks_since_move": 3, "offset_times": 0, "clicks_since_reset": 0}
        options = {"every": 3, "direction": "向下", "reset_after": 0}
        msg = self.h.coord_step_log_message(0, 0, state, options)
        # next_after = 3 - 3 = 0 -> wraps to every=3
        self.assertIn("第3次后进行下次偏移", msg)


if __name__ == "__main__":
    unittest.main()
