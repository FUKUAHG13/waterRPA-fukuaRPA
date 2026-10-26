import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib
import unittest

import numpy as np

from fukua_rpa.engine import RPAEngine

# The six focused mixin submodules produced by the P4a split of the old
# monolithic ``engine_vision.py``. The facade (``engine_vision``) must keep
# composing all of them and preserve the public contract consumed by
# ``RPAEngine``.
SUBMODULES = [
    "vision.shared",
    "vision.preload",
    "vision.match",
    "vision.scene",
    "vision.multi_image",
    "vision.adaptive",
]
# The mixin class for ``vision.<x>`` is ``Vision<X>Mixin`` (e.g. vision.multi_image
# -> VisionMultiImageMixin).
MIXIN_NAMES = [
    "".join(part.capitalize() for part in name.replace(".", "_").split("_")) + "Mixin"
    for name in SUBMODULES
]


def make_engine():
    """Create a minimal RPAEngine without initializing real backends."""
    engine = RPAEngine(defer_backends=True)
    engine.log_level = -1
    return engine


class EngineVisionSplitTests(unittest.TestCase):
    """Guard rails for the P4a engine_vision split (facade + mixin submodules)."""

    def test_submodules_import_cleanly(self):
        """Each split submodule imports without circular-import or missing deps."""
        for name in SUBMODULES:
            mod = importlib.import_module(f"fukua_rpa.{name}")
            self.assertTrue(hasattr(mod, MIXIN_NAMES[SUBMODULES.index(name)]))

    def test_facade_composes_all_mixins(self):
        """The facade MRO must contain every split mixin, in order."""
        from fukua_rpa.engine.vision_exec import VisionExecutionMixin

        mro = [c.__name__ for c in VisionExecutionMixin.__mro__]
        for mixin in MIXIN_NAMES:
            self.assertIn(mixin, mro)
        # object is the ultimate base; facade itself is first.
        self.assertEqual(mro[0], "VisionExecutionMixin")
        self.assertEqual(mro[-1], "object")

    def test_facade_preserves_module_level_reexports(self):
        """External/test patches on the old module-level command sets still work."""
        import fukua_rpa.engine.vision_exec as ev

        self.assertTrue(hasattr(ev, "CLICK_COMMANDS"))
        self.assertTrue(hasattr(ev, "IMAGE_TARGET_COMMANDS"))
        self.assertIn(1.0, ev.IMAGE_TARGET_COMMANDS)

    def test_engine_still_constructs(self):
        """RPAEngine constructed via the facade still exposes vision behavior."""
        engine = make_engine()
        self.assertTrue(hasattr(engine, "match_template_scores"))
        self.assertTrue(callable(engine.match_template_scores))

    def test_match_template_scores_shape(self):
        """match_template_scores keeps its pre-split output contract."""
        engine = make_engine()
        np.random.seed(20260811)  # FR-20260809-1404: 固定种子保证可复现
        screen = np.random.randint(0, 255, (64, 64), dtype=np.uint8)
        template = np.random.randint(0, 255, (16, 16), dtype=np.uint8)
        result, nan_mask = engine.match_template_scores(screen, template)
        self.assertIsNone(nan_mask)
        self.assertEqual(result.shape, (49, 49))


if __name__ == "__main__":
    unittest.main()
