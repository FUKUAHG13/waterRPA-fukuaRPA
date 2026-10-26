import unittest

from fukua_rpa.vision.scale_memory import (
    ScaleMemoryPolicy,
    ScaleMemoryStore,
    parse_manual_scales,
)


class ScaleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.key = ("template.png", 1)
        self.valid = (0.8, 0.9, 1.0, 1.1, 1.2)

    def test_manual_scales_are_deduplicated_and_bounded(self):
        self.assertEqual(
            parse_manual_scales("0.8, 1.0；0.8 1.2"),
            (0.8, 1.0, 1.2),
        )
        with self.assertRaises(ValueError):
            parse_manual_scales("0")
        with self.assertRaises(ValueError):
            parse_manual_scales(" ".join(str(0.1 + i * 0.1) for i in range(13)))

    def test_read_only_lookups_are_bounded_and_count_as_recent_use(self):
        """REVIEW-20260913-001 FR-003：只读 ``preferred_scales`` 也必须受
        ``max_entries`` 约束，并把条目记为最近使用。

        此前只读路径插入后既不回收也不刷新 ``updated_sequence``：``_entries``
        可突破上限，且这些 sequence 恒为 0 的条目会在任意一次 ``record`` 触发的
        回收中被集体优先淘汰（既非 LRU 也非预期语义）。"""
        store = ScaleMemoryStore(max_entries=4)
        policy = ScaleMemoryPolicy(tier="balanced")

        for index in range(20):
            store.preferred_scales(
                (f"t{index}.png", 1), f"t{index}.png", self.valid, policy
            )

        self.assertLessEqual(len(store._entries), 4)
        labels = {summary.label for summary in store.summaries(policy, maximum=8)}
        self.assertEqual(
            labels,
            {"t16.png", "t17.png", "t18.png", "t19.png"},
            "只读访问应按最近使用保留，而不是保留最早插入的条目",
        )

    def test_record_after_read_only_lookups_keeps_recent_summaries(self):
        """只读与 record 混用时，``summaries`` 仍返回最近使用的条目。"""
        store = ScaleMemoryStore(max_entries=4)
        policy = ScaleMemoryPolicy(tier="balanced")
        for index in range(6):
            store.preferred_scales(
                (f"t{index}.png", 1), f"t{index}.png", self.valid, policy
            )

        store.record(("t5.png", 1), "t5.png", self.valid, 1.0, 0.95, policy)

        labels = {summary.label for summary in store.summaries(policy, maximum=8)}
        self.assertLessEqual(len(labels), 4)
        self.assertIn("t5.png", labels, "最近记录的条目必须存活")

    def test_manual_scales_precede_frequency_ranked_history(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy(
            tier="balanced", manual_scales=(0.9, 1.2)
        )
        for scale in (1.0, 1.0, 1.2, 1.0, 0.8, 1.0, 1.2):
            store.record(self.key, "template.png", self.valid, scale, 0.95, policy)
        preferred, summary = store.preferred_scales(
            self.key, "template.png", self.valid, policy
        )
        self.assertEqual(preferred[:2], (0.9, 1.2))
        self.assertIn(1.0, preferred)
        self.assertEqual(summary.manual_scales, (0.9, 1.2))
        self.assertGreaterEqual(summary.history_limit, summary.observed_count)

    def test_auto_tiers_change_dynamic_tendency_not_fixed_values(self):
        store = ScaleMemoryStore()
        balanced = ScaleMemoryPolicy(tier="balanced")
        for scale in (0.8, 0.9, 1.0, 1.1, 1.2) * 5:
            store.record(self.key, "template.png", self.valid, scale, 0.9, balanced)
        conservative = store.summaries(
            ScaleMemoryPolicy(tier="conservative")
        )[0]
        aggressive = store.summaries(
            ScaleMemoryPolicy(tier="aggressive")
        )[0]
        self.assertGreaterEqual(aggressive.history_limit, conservative.history_limit)
        self.assertGreaterEqual(
            len(aggressive.learned_scales), len(conservative.learned_scales)
        )

    def test_custom_limits_trim_history_and_bound_learned_scales(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy(
            custom_enabled=True,
            preferred_limit=2,
            history_limit=8,
        )
        summary = None
        for scale in self.valid * 6:
            summary, _changed = store.record(
                self.key, "template.png", self.valid, scale, 0.9, policy
            )
        self.assertEqual(summary.history_limit, 8)
        self.assertEqual(summary.observed_count, 8)
        self.assertEqual(len(summary.learned_scales), 2)

    def test_unavailable_manual_scale_is_ignored_for_this_grid(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy(manual_scales=(0.85, 1.0))
        preferred, summary = store.preferred_scales(
            self.key, "template.png", self.valid, policy
        )
        self.assertEqual(preferred, (1.0,))
        self.assertEqual(summary.manual_scales, (1.0,))

    def test_range_change_reuses_entry_and_keeps_only_valid_history(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy()
        generation = (100, 2048)
        for scale in (0.8, 1.0, 1.2, 1.2):
            store.record(
                self.key,
                "template.png",
                self.valid,
                scale,
                0.95,
                policy,
                generation=generation,
            )

        narrowed = (0.9, 1.0, 1.1)
        preferred, summary = store.preferred_scales(
            self.key,
            "template.png",
            narrowed,
            policy,
            generation=generation,
        )

        self.assertEqual(len(store.summaries(policy)), 1)
        self.assertEqual(summary.observed_count, 1)
        self.assertEqual(summary.unique_count, 1)
        self.assertEqual(preferred, (1.0,))

    def test_generation_change_resets_observations_in_same_entry(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy()
        store.record(
            self.key,
            "template.png",
            self.valid,
            1.2,
            0.95,
            policy,
            generation=(100, 2048),
        )

        preferred, summary = store.preferred_scales(
            self.key,
            "template.png",
            self.valid,
            policy,
            generation=(101, 2048),
        )

        self.assertEqual(preferred, ())
        self.assertEqual(summary.observed_count, 0)
        self.assertEqual(len(store.summaries(policy)), 1)

    def test_first_known_generation_does_not_discard_legacy_history(self):
        store = ScaleMemoryStore()
        policy = ScaleMemoryPolicy()
        store.record(
            self.key, "template.png", self.valid, 1.1, 0.95, policy
        )

        preferred, summary = store.preferred_scales(
            self.key,
            "template.png",
            self.valid,
            policy,
            generation=(100, 2048),
        )

        self.assertIn(1.1, preferred)
        self.assertEqual(summary.observed_count, 1)


if __name__ == "__main__":
    unittest.main()
