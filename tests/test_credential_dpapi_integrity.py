"""Evidence test for DPAPI integrity protection (ADV-001).

ADV-001 asked whether an explicit integrity check should be added on top of
DPAPI. This test settles it with evidence instead of speculation: if DPAPI
already rejects tampered ciphertext, no production code change is needed and
the item closes with recorded evidence.

Run with ``python -m unittest -v tests.test_credential_dpapi_integrity``.
Requires Windows (DPAPI is user-bound); skipped elsewhere.
"""

import base64
import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa.credentials import (
    CredentialStoreError,
    protect_secret,
    unprotect_secret,
)


@unittest.skipUnless(os.name == "nt", "DPAPI 仅存在于 Windows")
class DpapiIntegrityTests(unittest.TestCase):
    """DPAPI must detect tampering without an application-level checksum."""

    def test_round_trip_preserves_secret(self):
        secret = "correct horse battery staple"
        self.assertEqual(unprotect_secret(protect_secret(secret)), secret)

    def test_tampered_ciphertext_is_rejected(self):
        """Flipping ciphertext bytes must fail, never yield altered plaintext."""
        secret = "correct horse battery staple"
        encoded = protect_secret(secret)
        raw = bytearray(base64.b64decode(encoded))

        # 翻转密文中部字节，避开描述/头部结构，模拟攻击者篡改密文。
        index = len(raw) // 2
        raw[index] ^= 0xFF
        tampered = base64.b64encode(bytes(raw)).decode("ascii")

        with self.assertRaises(CredentialStoreError):
            unprotect_secret(tampered)

    def test_truncated_ciphertext_is_rejected(self):
        """Truncating the blob must fail rather than decrypt partially."""
        secret = "correct horse battery staple"
        raw = base64.b64decode(protect_secret(secret))
        truncated = base64.b64encode(raw[: max(1, len(raw) - 16)]).decode("ascii")

        with self.assertRaises(CredentialStoreError):
            unprotect_secret(truncated)

    def test_tampering_cannot_yield_different_plaintext(self):
        """The strongest claim: tampering never returns a *different* secret."""
        secret = "correct horse battery staple"
        raw = bytearray(base64.b64decode(protect_secret(secret)))
        raw[len(raw) // 2] ^= 0xFF
        tampered = base64.b64encode(bytes(raw)).decode("ascii")

        try:
            recovered = unprotect_secret(tampered)
        except CredentialStoreError:
            return  # 期望路径：内建完整性校验已足够
        # 若竟然解密成功，也不得返回与原文不同的内容（否则完整性已失守）。
        self.assertEqual(recovered, secret)


class CredentialStoreSizeGuardTests(unittest.TestCase):
    """FR-RISK-20261010-2501：写侧必须有与读侧对称的总量守卫。

    读侧 ``load_bounded_json_file(max_bytes=MAX_CREDENTIAL_FILE_BYTES)`` 是硬
    上限；修复前写侧无守卫，合法规模（64 条接近 1MB 的秘密）即可写出超限文件，
    下次启动 names/get/set/delete 全部失败——整体自锁（隔离探针
    probe_credential_lock_red.py 已复现）。本组用例注入恒等 protect/unprotect，
    只验证存储逻辑，不依赖 DPAPI。
    """

    def _store(self, tmp):
        from fukua_rpa.credentials import CredentialStore

        return CredentialStore(tmp, protect=lambda s: s, unprotect=lambda s: s)

    def _seed_near_limit(self, store):
        """直接落盘 64 条 ~1MB（模拟旧版本可写出的边界状态：文件仍低于读侧
        64MB 上限、可正常读取）。"""
        from fukua_rpa.config_store import atomic_write_json
        from fukua_rpa.credentials import (
            CREDENTIAL_FORMAT,
            CREDENTIAL_VERSION,
            MAX_SECRET_BYTES,
        )

        big = "x" * (MAX_SECRET_BYTES - 64)
        atomic_write_json(
            store.path,
            {
                "format": CREDENTIAL_FORMAT,
                "version": CREDENTIAL_VERSION,
                "credentials": {f"k{i:03d}": big for i in range(64)},
            },
        )

    def test_set_rejects_write_beyond_read_limit(self):
        from fukua_rpa.credentials import MAX_SECRET_BYTES

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._seed_near_limit(store)
            # 前置条件：种子库仍可读（低于 64MB）。
            self.assertEqual(len(store.names()), 64)
            big = "x" * (MAX_SECRET_BYTES - 64)
            with self.assertRaises(CredentialStoreError) as ctx:
                store.set("overflow", big)
            self.assertIn("凭据库已满", str(ctx.exception))
            # 拒绝发生在写盘前：库仍可读、原条目保留、未产生超限文件。
            self.assertEqual(len(store.names()), 64)
            self.assertLess(
                os.path.getsize(store.path), 64 * 1024 * 1024
            )

    def test_normal_scale_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set("a", "x" * 1024)
            store.set("b", "y" * 2048)
            self.assertEqual(store.get("a"), "x" * 1024)
            self.assertEqual(store.get("b"), "y" * 2048)
            self.assertEqual(store.names(), ["a", "b"])
            store.delete("a")
            self.assertEqual(store.names(), ["b"])

    def test_overwrite_existing_name_within_limit_succeeds(self):
        """同名额改不新增体积（近似），不得被守卫误拒。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set("a", "x" * 4096)
            store.set("a", "y" * 4096)
            self.assertEqual(store.get("a"), "y" * 4096)

    def test_on_disk_size_equals_projected_size(self):
        """FR-20261011-010：落盘字节必须与 ``_projected_size`` 的序列化口径完全
        一致。文本模式会把 ``\\n`` 写成 ``\\r\\n``，实际文件比投影多出换行符个数，
        极端边界下会写出读侧（``getsize > 64MB``）拒绝读回的凭据库。"""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set("a", "x" * 4096)
            store.set("b", "y" * 2048)
            with open(store.path, "rb") as handle:
                raw = handle.read()
            self.assertNotIn(b"\r\n", raw)
            payload = json.loads(raw.decode("utf-8"))
            projected = len(
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            )
            self.assertEqual(os.path.getsize(store.path), projected)

    def test_boundary_write_stays_readable_when_projection_hits_limit(self):
        """FR-20261011-010 边界：把读侧上限设为恰好等于投影大小时，写入必须成功
        且随后仍能读回；旧实现因换行翻译写出更大文件而自锁。"""
        import json

        from fukua_rpa import credentials as credentials_module

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.set("a", "x" * 4096)
            with open(store.path, encoding="utf-8") as handle:
                payload = json.loads(handle.read())
            projected = len(
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            )
            with mock.patch.object(
                credentials_module, "MAX_CREDENTIAL_FILE_BYTES", projected
            ):
                store.set("a", "x" * 4096)
                self.assertEqual(store.names(), ["a"])
                self.assertEqual(store.get("a"), "x" * 4096)
            self.assertEqual(os.path.getsize(store.path), projected)


if __name__ == "__main__":
    unittest.main()
