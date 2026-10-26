import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

from PIL import Image

from fukua_rpa.constants import TASK_TYPE_MULTI_IMAGE_ACTION, TASK_TYPE_UNTIL
from fukua_rpa.engine import RPAEngine


def make_engine():
    """Create a minimal RPAEngine without initializing real backends."""
    engine = RPAEngine(defer_backends=True)
    engine.log_level = -1
    return engine


class PrecacheAbortP2_1Tests(unittest.TestCase):
    """P2-1 / §3.10: a missing or corrupt template must ABORT the run.

    Previously a referenced-but-missing template image was silently skipped,
    which left the recognition step to wait forever in the ``reTry == -1``
    loop. The fix makes ``load_and_precompute`` fail (return False) on a bad
    template, and ``RPAEngine._prepare_run`` aborts (returns None) instead of
    proceeding into stepping/infinite-wait.
    """

    def setUp(self):
        self.engine = make_engine()
        self.engine.opencv_available = True

    # --- Unit level: precompute must fail on bad templates -----------------

    def test_missing_image_template_aborts_precompute(self):
        """A missing image-target template fails precompute (P2-1)."""
        tasks = [
            {
                "type": 1,
                "value": "/nonexistent/p2_1_missing_template.png",
                "step_disabled": False,
            }
        ]
        with mock.patch(
            "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertFalse(
            result,
            "missing template must fail precompute, not be silently skipped",
        )

    def test_corrupt_image_template_aborts_precompute(self):
        """An existing-but-unreadable template fails precompute (P2-1)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "corrupt.png")
            with open(path, "wb") as fh:
                fh.write(b"this is not a valid image file")
            tasks = [
                {
                    "type": 1,
                    "value": path,
                    "step_disabled": False,
                }
            ]
            with mock.patch(
                "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertFalse(
            result,
            "corrupt template must fail precompute, not flow into infinite wait",
        )

    def test_missing_until_condition_template_aborts_precompute(self):
        """A missing 'until' condition template fails precompute (P2-1)."""
        tasks = [
            {
                "type": TASK_TYPE_UNTIL,
                "until_cond1_en": True,
                "until_cond1_mode": "图片出现",
                "until_cond1_image": "/nonexistent/p2_1_until.png",
                "until_cond1_region": "",
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertFalse(
            result,
            "missing until-condition template must fail precompute",
        )

    def test_missing_multi_image_template_aborts_precompute(self):
        """A missing multi-image-action template fails precompute (P2-1)."""
        tasks = [
            {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "multi_image_actions": [
                    {"image": "/nonexistent/p2_1_multi.png", "action": "left_click"}
                ],
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertFalse(
            result,
            "missing multi-image template must fail precompute",
        )

    def test_valid_template_still_precomputes(self):
        """Sanity control: a valid template still succeeds (no false abort)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            img = Image.new("RGB", (24, 32))
            pixels = img.load()
            for x in range(24):
                for y in range(32):
                    pixels[x, y] = ((x * 37) % 256, (y * 53) % 256, 128)
            img.save(path)
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    # --- Integration level: the run aborts before stepping -----------------

    def test_prepare_run_aborts_when_preload_fails(self):
        """_prepare_run returns None (abort) instead of entering infinite wait.

        A genuine missing template makes ``load_and_precompute`` return False,
        so ``_prepare_run`` must abort and never proceed to the run loop.
        Heavy runtime setup/teardown is stubbed so the test isolates the
        abort decision.
        """
        tasks = [
            {
                "type": 1,
                "value": "/nonexistent/p2_1_abort_run.png",
                "step_disabled": False,
            }
        ]
        with mock.patch(
            "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
        ), mock.patch.object(
            self.engine, "_initialize_run_runtime"
        ), mock.patch.object(
            self.engine, "_abort_preload_run"
        ) as abort_spy:
            run_id = self.engine.reserve_run()
            result = self.engine._prepare_run(
                tasks,
                run_id=run_id,
                callback_msg=None,
                callback_status=None,
                callback_click_indicator=None,
                callback_debug=None,
                callback_prompt=None,
            )

        self.assertIsNone(
            result,
            "run must abort (return None) when template preload fails",
        )
        abort_spy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
