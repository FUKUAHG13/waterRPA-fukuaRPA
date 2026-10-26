"""插件协议信封校验回归测试。

FR-009：validate_plugin_message 的 request 分支用
``dict(payload.get("payload") or {})`` 收窄内层 payload 类型，对不可转 dict 的
真值会泄漏裸 TypeError / ValueError，而模块文档承诺只抛 PluginProtocolError
（ARCHITECTURE #86 要求可分类错误处理）。
"""

import unittest

from fukua_rpa.plugin_protocol import (
    PLUGIN_PROTOCOL_NAME,
    PLUGIN_PROTOCOL_VERSION,
    PluginProtocolError,
    deframe_message,
    frame_message,
    validate_plugin_message,
)


def _request(inner_payload):
    """构造一个除内层 payload 外全部合法的 request 信封。"""

    return {
        "protocol": PLUGIN_PROTOCOL_NAME,
        "version": PLUGIN_PROTOCOL_VERSION,
        "kind": "request",
        "payload": {
            "run_id": "run-1",
            "step_id": "step-1",
            "plugin_id": "demo",
            "action_id": "act",
            "deadline_ns": 1_000_000,
            "payload": inner_payload,
        },
    }


class RequestPayloadTypeTests(unittest.TestCase):
    def test_list_payload_raises_protocol_error(self):
        with self.assertRaises(PluginProtocolError):
            validate_plugin_message(_request([1, 2]))

    def test_string_payload_raises_protocol_error(self):
        with self.assertRaises(PluginProtocolError):
            validate_plugin_message(_request("abc"))

    def test_int_payload_raises_protocol_error(self):
        with self.assertRaises(PluginProtocolError):
            validate_plugin_message(_request(5))

    def test_mapping_payload_is_accepted(self):
        """对照组：合法 Mapping 必须照常通过。"""

        result = validate_plugin_message(_request({"a": 1}))
        self.assertEqual(result["payload"]["payload"], {"a": 1})

    def test_missing_payload_defaults_to_empty_mapping(self):
        """对照组：内层 payload 缺失/为 None 时回退空字典。"""

        self.assertEqual(
            validate_plugin_message(_request(None))["payload"]["payload"], {}
        )

    def test_pair_list_is_now_rejected(self):
        """契约收紧：pair-list 曾被 dict() 接受，收紧后必须被拒绝。"""

        with self.assertRaises(PluginProtocolError):
            validate_plugin_message(_request([("a", 1)]))


class NonFiniteFrameTests(unittest.TestCase):
    """FR-004：入站帧必须拒绝 NaN/Infinity，与出站 ``allow_nan=False`` 对称。

    修复前 ``deframe_message`` 使用裸 ``json.loads``（默认接受 NaN/Infinity），
    而出站 ``frame_message`` 明确拒绝——同一协议两个方向语义相反，违反
    ARCHITECTURE #61「解析时拒绝重复键、NaN 与 Infinity」。
    """

    @staticmethod
    def _framed(json_text: str) -> bytes:
        raw = json_text.encode("utf-8")
        return len(raw).to_bytes(4, "big") + raw

    @staticmethod
    def _response(result_literal: str) -> str:
        return (
            '{"protocol":"fukuaRPA.plugin","version":1,"kind":"response",'
            '"payload":{"run_id":"r1","step_id":"s1","ok":true,'
            '"result":' + result_literal + "}}"
        )

    def test_nan_in_inbound_frame_is_rejected(self):
        with self.assertRaises(PluginProtocolError):
            deframe_message(self._framed(self._response("NaN")))

    def test_infinity_variants_in_inbound_frame_are_rejected(self):
        for token in ("Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaises(PluginProtocolError):
                deframe_message(self._framed(self._response(token)))

    def test_duplicate_keys_are_still_rejected(self):
        """对照：既有重复键拒绝行为不变。"""

        envelope = (
            '{"protocol":"fukuaRPA.plugin","version":1,"kind":"response",'
            '"payload":{"run_id":"r1","run_id":"r2","step_id":"s1","ok":true}}'
        )
        with self.assertRaises(PluginProtocolError):
            deframe_message(self._framed(envelope))

    def test_outbound_non_finite_encoding_is_still_rejected(self):
        """对照：出站方向行为不变。"""

        message = {
            "protocol": PLUGIN_PROTOCOL_NAME,
            "version": PLUGIN_PROTOCOL_VERSION,
            "kind": "response",
            "payload": {
                "run_id": "r1",
                "step_id": "s1",
                "ok": True,
                "result": float("nan"),
            },
        }
        with self.assertRaises(PluginProtocolError):
            frame_message(message)

    def test_legal_frame_still_round_trips(self):
        """正向对照：合法帧仍可正常解帧。"""

        message = {
            "protocol": PLUGIN_PROTOCOL_NAME,
            "version": PLUGIN_PROTOCOL_VERSION,
            "kind": "response",
            "payload": {
                "run_id": "r1",
                "step_id": "s1",
                "ok": True,
                "result": {"a": 1},
            },
        }
        decoded = deframe_message(frame_message(message))
        self.assertEqual(decoded["payload"]["result"], {"a": 1})


if __name__ == "__main__":
    unittest.main()
