import threading
import time
import unittest

from fukua_rpa.debug_session import DebugSession


class DebugSessionTests(unittest.TestCase):
    def wait_until(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_breakpoint_blocks_until_continue(self):
        session = DebugSession()
        session.reset()
        result = []
        events = []
        worker = threading.Thread(
            target=lambda: result.append(
                session.before_step(
                    step=2,
                    loop=3,
                    command="左键单击",
                    breakpoint=True,
                    callback=events.append,
                )
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(self.wait_until(lambda: session.snapshot().paused))
        self.assertTrue(session.continue_run())
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0][0])
        self.assertEqual(events[0]["state"], "paused")
        self.assertEqual(events[-1]["state"], "resumed")

    def test_step_over_pauses_before_the_following_step(self):
        session = DebugSession()
        session.reset()
        first = threading.Thread(
            target=lambda: session.before_step(
                step=1, loop=1, command="输入文本", breakpoint=True
            ),
            daemon=True,
        )
        first.start()
        self.assertTrue(self.wait_until(lambda: session.snapshot().paused))
        self.assertTrue(session.step_over())
        first.join(1.0)

        second_result = []
        second = threading.Thread(
            target=lambda: second_result.append(
                session.before_step(
                    step=2, loop=1, command="等待", breakpoint=False
                )
            ),
            daemon=True,
        )
        second.start()
        self.assertTrue(self.wait_until(lambda: session.snapshot().paused))
        self.assertEqual(session.snapshot().reason, "step_over")
        session.continue_run()
        second.join(1.0)
        self.assertTrue(second_result[0][0])

    def test_cancel_wakes_a_paused_run(self):
        session = DebugSession()
        session.reset()
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                session.before_step(
                    step=1, loop=1, command="等待", breakpoint=True
                )
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(self.wait_until(lambda: session.snapshot().paused))
        session.cancel()
        worker.join(1.0)
        self.assertEqual(result[0][0], False)

    def test_reset_is_idempotent_across_repeated_calls(self):
        session = DebugSession()
        session.reset()
        baseline = session.snapshot()
        self.assertTrue(baseline.active)
        self.assertFalse(baseline.paused)
        for _ in range(3):
            session.reset()
            self.assertEqual(session.snapshot(), baseline)

    def test_concurrent_before_step_calls_do_not_deadlock(self):
        session = DebugSession()
        session.reset()
        results = []
        results_lock = threading.Lock()

        def worker(step):
            for loop in range(50):
                outcome = session.before_step(
                    step=step, loop=loop, command="并发", breakpoint=False
                )
                with results_lock:
                    results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 100)
        self.assertTrue(all(allowed for allowed, _duration in results))

    def test_conditional_breakpoint_surfaces_reason_and_bounded_detail(self):
        session = DebugSession()
        session.reset()
        events = []
        long_detail = "x" * 1000
        worker = threading.Thread(
            target=lambda: session.before_step(
                step=3,
                loop=1,
                command="条件断点",
                breakpoint=True,
                breakpoint_reason="condition",
                breakpoint_detail=long_detail,
                callback=events.append,
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(self.wait_until(lambda: session.snapshot().paused))
        snapshot = session.snapshot()
        self.assertEqual(snapshot.reason, "condition")
        self.assertEqual(len(snapshot.detail), 500)
        self.assertTrue(session.continue_run())
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(events[0]["reason"], "condition")
        self.assertEqual(events[-1]["state"], "resumed")


if __name__ == "__main__":
    unittest.main()
