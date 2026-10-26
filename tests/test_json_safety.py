import os
import tempfile
import unittest

from fukua_rpa.json_safety import (
    load_bounded_json_file,
    loads_bounded_json,
    validate_json_structure,
)


class BoundedJsonTests(unittest.TestCase):
    def test_accepts_normal_utf8_json(self):
        parsed = loads_bounded_json(
            '{"名称": "方案", "步骤": [1, true, null]}',
            max_bytes=1024,
        )

        self.assertEqual(parsed["名称"], "方案")
        self.assertEqual(parsed["步骤"], [1, True, None])

    def test_rejects_payload_larger_than_byte_limit(self):
        with self.assertRaisesRegex(ValueError, "大小超过上限"):
            loads_bounded_json('{"value": "12345"}', max_bytes=8)

    def test_rejects_duplicate_keys_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(ValueError, "重复字段"):
            loads_bounded_json('{"value": 1, "value": 2}', max_bytes=128)
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), self.assertRaisesRegex(
                ValueError, "无效数字"
            ):
                loads_bounded_json(constant, max_bytes=128)

    def test_rejects_excessive_depth_and_node_count(self):
        deeply_nested = "[" * 20 + "0" + "]" * 20
        with self.assertRaisesRegex(ValueError, "嵌套"):
            loads_bounded_json(
                deeply_nested,
                max_bytes=1024,
                max_depth=8,
            )
        with self.assertRaisesRegex(ValueError, "节点数量"):
            loads_bounded_json(
                "[0, 1, 2, 3]",
                max_bytes=1024,
                max_nodes=4,
            )

    def test_iterative_validator_rejects_cycles_and_invalid_keys(self):
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaisesRegex(ValueError, "循环引用"):
            validate_json_structure(cyclic)
        with self.assertRaisesRegex(ValueError, "非文本字段名"):
            validate_json_structure({1: "value"})

    def test_file_parser_checks_size_before_parsing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "large.json")
            with open(path, "wb") as handle:
                handle.write(b"{" + b" " * 64 + b"}")

            with self.assertRaisesRegex(ValueError, "大小超过上限"):
                load_bounded_json_file(path, max_bytes=16)


if __name__ == "__main__":
    unittest.main()
