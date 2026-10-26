"""Property-based tests for fukua_rpa.json_safety using Hypothesis.

These complement the example-based tests in test_json_safety.py by exploring
boundary combinations of nesting depth, node count, byte size, duplicate keys,
non-string keys, and non-finite numbers. The whole module is skipped when
Hypothesis is not installed (it is a development-only dependency, see
requirements-dev.txt).
"""

from __future__ import annotations

import json
import unittest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:  # Hypothesis 仅用于开发期属性化测试，缺失时整体跳过。
    _HAS_HYPOTHESIS = False


if _HAS_HYPOTHESIS:
    from fukua_rpa.json_safety import loads_bounded_json, validate_json_structure

    # JSON 可序列化值的递归策略：基元 + 列表 + 字符串键字典。
    _json_primitives = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=20),
    )
    _json_values = st.recursive(
        _json_primitives,
        lambda children: st.one_of(
            st.lists(children, max_size=5),
            st.dictionaries(st.text(max_size=10), children, max_size=5),
        ),
        max_leaves=30,
    )
    # database=None 避免向磁盘写入示例库；deadline=None 避免计时抖动导致偶发失败。
    _settings = settings(database=None, max_examples=50, deadline=None)

    class JsonSafetyPropertyTests(unittest.TestCase):
        """Hypothesis 属性化测试：探索有界 JSON 解析的边界组合。"""

        @_settings
        @given(_json_values)
        def test_validate_accepts_valid_bounded_structures(self, value):
            validate_json_structure(value, max_depth=100, max_nodes=100000)

        @_settings
        @given(_json_values)
        def test_round_trip_through_json_text(self, value):
            text = json.dumps(value, ensure_ascii=False)
            parsed = loads_bounded_json(
                text,
                max_bytes=len(text.encode("utf-8")) + 64,
                max_depth=100,
                max_nodes=100000,
            )
            self.assertEqual(parsed, value)

        @_settings
        @given(_json_values.filter(lambda v: not isinstance(v, str)))
        def test_preparsed_object_returned_unchanged(self, value):
            # str 被 loads_bounded_json 视为待解析 JSON 文本而非已解析值，
            # 故此处仅生成非 str 的已解析对象（dict/list/标量）。
            result = loads_bounded_json(
                value, max_bytes=10**9, max_depth=100, max_nodes=100000
            )
            self.assertEqual(result, value)

        @_settings
        @given(st.integers(min_value=1, max_value=40))
        def test_depth_limit_rejects_over_deep_nesting(self, limit):
            value = 0
            for _ in range(limit + 5):
                value = [value]
            with self.assertRaisesRegex(ValueError, "嵌套"):
                validate_json_structure(value, max_depth=limit, max_nodes=10**6)

        @_settings
        @given(st.integers(min_value=1, max_value=100))
        def test_node_limit_rejects_excessive_nodes(self, limit):
            value = list(range(limit + 5))
            with self.assertRaisesRegex(ValueError, "节点数量"):
                validate_json_structure(value, max_depth=100, max_nodes=limit)

        @_settings
        @given(
            st.sampled_from(["a", "名称", "key", "步骤"]),
            st.integers(min_value=-(10**6), max_value=10**6),
            st.integers(min_value=-(10**6), max_value=10**6),
        )
        def test_duplicate_keys_rejected(self, key, first, second):
            text = (
                json.dumps({key: first})[:-1]
                + ", "
                + json.dumps({key: second})[1:]
            )
            with self.assertRaisesRegex(ValueError, "重复字段"):
                loads_bounded_json(text, max_bytes=4096)

        @_settings
        @given(st.sampled_from([float("nan"), float("inf"), float("-inf")]))
        def test_nonfinite_floats_rejected(self, value):
            with self.assertRaisesRegex(ValueError, "NaN"):
                validate_json_structure(value)

        @_settings
        @given(
            st.one_of(
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.booleans(),
                st.none(),
            )
        )
        def test_non_string_dict_keys_rejected(self, key):
            with self.assertRaisesRegex(ValueError, "非文本字段名"):
                validate_json_structure({key: 1})

        @_settings
        @given(st.binary(min_size=2, max_size=200))
        def test_byte_limit_rejects_oversized_payload(self, data):
            with self.assertRaisesRegex(ValueError, "大小超过上限"):
                loads_bounded_json(data, max_bytes=len(data) - 1)


else:

    class JsonSafetyPropertyTests(unittest.TestCase):
        """Hypothesis 缺失时的占位用例：让"整体跳过"是**可见**的。

        没有这个分支，缺少 Hypothesis 的环境里本模块一个用例都定义不出来，
        unittest 收集到 0 个测试却仍以退出码 0 通过——跳过变成了静默漏测，
        与"测试通过"在门禁输出上无法区分。占位用例把它显式记为 skipped，
        同时满足 run_unittest_qt.py 的零用例熔断。
        """

        @unittest.skip(
            "Hypothesis 未安装（见 requirements-dev.txt），属性化测试整体跳过"
        )
        def test_hypothesis_property_suite_skipped(self):
            pass


if __name__ == "__main__":
    unittest.main()
