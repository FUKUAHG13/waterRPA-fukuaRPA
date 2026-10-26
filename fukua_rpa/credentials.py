"""User-scoped DPAPI credential storage; workflow files keep names only."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, cast

from .config_store import atomic_write_json
from .json_safety import load_bounded_json_file

CREDENTIAL_FILE_NAME = "credentials.dat"
CREDENTIAL_FORMAT = "fukuaRPA_dpapi_credentials"
CREDENTIAL_VERSION = 1
MAX_CREDENTIALS = 256
MAX_CREDENTIAL_NAME_LENGTH = 100
MAX_SECRET_BYTES = 1024 * 1024
MAX_PROTECTED_TEXT_LENGTH = 2 * 1024 * 1024
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024 * 1024
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialStoreError(RuntimeError):
    """Raised when a credential operation fails (DPAPI, validation, or I/O)."""


class DATA_BLOB(ctypes.Structure):
    """ctypes mirror of Win32 DATA_BLOB for CryptProtectData/CryptUnprotectData."""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p


def _input_blob(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    source, _source_buffer = _input_blob(data)
    result = DATA_BLOB()
    error_code = 0
    ctypes.set_last_error(0)
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(source),
            "fukuaRPA credential",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        )
    else:
        description = wintypes.LPWSTR()
        success = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        )
        error_code = ctypes.get_last_error()
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
    if protect:
        error_code = ctypes.get_last_error()
    if not success:
        raise CredentialStoreError(
            f"Windows DPAPI 操作失败（错误码 {error_code}）"
        )
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if result.pbData:
            kernel32.LocalFree(result.pbData)


def protect_secret(secret: str) -> str:
    """Encrypt a secret string using Windows DPAPI and return base64 text.

    The encrypted blob is bound to the current Windows user profile, so only
    the same user on the same machine can decrypt it.

    Args:
        secret: The plaintext secret to protect.

    Returns:
        A base64-encoded string containing the DPAPI-encrypted payload.

    Raises:
        CredentialStoreError: If the secret exceeds MAX_SECRET_BYTES or the
            DPAPI call fails.
    """
    raw = str(secret).encode("utf-8")
    if len(raw) > MAX_SECRET_BYTES:
        raise CredentialStoreError("秘密内容过长")
    return base64.b64encode(_dpapi_transform(raw, True)).decode("ascii")


def unprotect_secret(encoded: str) -> str:
    """Decrypt a base64-encoded DPAPI blob back to the original plaintext.

    Args:
        encoded: Base64 string previously produced by :func:`protect_secret`.

    Returns:
        The decrypted plaintext secret.

    Raises:
        CredentialStoreError: If the payload is too large, corrupted, or was
            encrypted by a different Windows user.
    """
    try:
        if len(str(encoded)) > MAX_PROTECTED_TEXT_LENGTH:
            raise CredentialStoreError("凭据内容超过安全上限")
        protected = base64.b64decode(str(encoded), validate=True)
        # NOTE: Python str objects cannot be reliably zeroed from memory.
        # The decrypted plaintext remains in the interned string pool until GC.
        # This is a CPython limitation; the window is bounded by GC cycles.
        return _dpapi_transform(protected, False).decode("utf-8")
    except CredentialStoreError:
        raise
    except Exception as error:
        raise CredentialStoreError("凭据内容损坏或不属于当前 Windows 用户") from error


def validate_credential_name(name: str) -> str:
    """Validate and normalize a credential name.

    Strips leading/trailing whitespace and rejects empty names, names longer
    than MAX_CREDENTIAL_NAME_LENGTH, and names containing control characters.

    Args:
        name: The raw credential name to validate.

    Returns:
        The trimmed, validated name.

    Raises:
        CredentialStoreError: If the name is empty, too long, or contains
            control characters (code point < 32).
    """
    normalized = str(name or "").strip()
    if not normalized or len(normalized) > MAX_CREDENTIAL_NAME_LENGTH:
        raise CredentialStoreError(
            f"凭据名称长度必须为 1 到 {MAX_CREDENTIAL_NAME_LENGTH} 个字符"
        )
    if any(ord(char) < 32 for char in normalized):
        raise CredentialStoreError("凭据名称不能包含控制字符")
    return normalized


class CredentialStore:
    """Thread-safe, file-backed credential store using Windows DPAPI encryption.

    Secrets are encrypted with DPAPI (user-scoped) and persisted as a JSON
    file (``credentials.dat``) under *base_dir*. Workflow files reference
    credentials by name only; plaintext secrets never leave this store except
    through :meth:`get`.

    All public methods acquire an internal reentrant lock, making the store
    safe for concurrent access from UI and execution threads.
    """

    def __init__(self, base_dir: str, protect: Callable[[str], str] = protect_secret, unprotect: Callable[[str], str] = unprotect_secret) -> None:
        self.path = os.path.join(os.path.abspath(base_dir), CREDENTIAL_FILE_NAME)
        self._protect = protect
        self._unprotect = unprotect
        self._lock = threading.RLock()

    def _load_encrypted(self) -> dict[str, str]:
        if not os.path.isfile(self.path):
            return {}
        try:
            payload = load_bounded_json_file(
                self.path,
                max_bytes=MAX_CREDENTIAL_FILE_BYTES,
                label="凭据库",
                max_depth=4,
                max_nodes=MAX_CREDENTIALS * 3 + 16,
            )
        except CredentialStoreError:
            raise
        except ValueError as error:
            raise CredentialStoreError(str(error)) from error
        except Exception as error:
            raise CredentialStoreError("凭据库无法读取，文件可能已损坏") from error
        if not isinstance(payload, dict):
            raise CredentialStoreError("凭据库根结构必须是对象")
        data = cast(dict[str, Any], payload)
        if data.get("format") != CREDENTIAL_FORMAT:
            raise CredentialStoreError("凭据库格式无效")
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError) as error:
            raise CredentialStoreError("凭据库版本无效") from error
        if version > CREDENTIAL_VERSION:
            raise CredentialStoreError(
                f"凭据库版本 {version} 高于本程序支持的版本 {CREDENTIAL_VERSION}"
            )
        if version <= 0:
            raise CredentialStoreError("凭据库版本无效")
        credentials_raw = data.get("credentials")
        if not isinstance(credentials_raw, dict):
            raise CredentialStoreError("凭据库条目格式无效")
        credentials = cast(dict[str, str], credentials_raw)
        if len(credentials) > MAX_CREDENTIALS:
            raise CredentialStoreError(
                f"凭据库包含超过 {MAX_CREDENTIALS} 项内容"
            )
        if any(not isinstance(name, str) for name in credentials):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise CredentialStoreError("凭据名称必须是文本")
        if any(not isinstance(value, str) for value in credentials.values()):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise CredentialStoreError("加密凭据内容必须是文本")
        if any(len(value) > MAX_PROTECTED_TEXT_LENGTH for value in credentials.values()):
            raise CredentialStoreError("加密凭据内容超过安全上限")
        # FR-20260811-D2-002: 归一化同名冲突显式报错，禁止静默覆盖。dict 推导会
        # 让 strip 后同名的两条（如 "key" 与 " key "）后者覆盖前者，凭据静默丢失。
        normalized: dict[str, str] = {}
        for name, value in credentials.items():
            key = validate_credential_name(name)
            if key in normalized:
                raise CredentialStoreError(
                    f"凭据库包含归一化后同名的条目（{name}），拒绝加载"
                )
            normalized[key] = value
        return normalized

    def _projected_size(self, values: dict[str, str]) -> int:
        """按与 :func:`io_utils.atomic_write_json` 完全一致的序列化口径估算
        落盘字节数（indent=2、ensure_ascii=False、UTF-8）。"""

        payload = {
            "format": CREDENTIAL_FORMAT,
            "version": CREDENTIAL_VERSION,
            "credentials": values,
        }
        return len(
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        )

    def _save_encrypted(self, values: dict[str, str]) -> None:
        atomic_write_json(
            self.path,
            {
                "format": CREDENTIAL_FORMAT,
                "version": CREDENTIAL_VERSION,
                "credentials": values,
            },
            restrict_acl=True,
            # FR-20261011-010：禁用换行翻译，使落盘字节与 _projected_size 的
            # 序列化口径完全一致。文本模式会把 '\n' 写成 '\r\n'，实际文件比投影
            # 多出换行符个数，极端边界下会写出读侧（getsize > 64MB）拒绝读回的
            # 凭据库，导致 names/get/set/delete 全部失败且无自救路径。
            newline="",
        )

    def names(self) -> list[str]:
        """Return all stored credential names sorted case-insensitively."""
        with self._lock:
            return sorted(self._load_encrypted(), key=str.casefold)

    def get(self, name: str) -> str:
        """Decrypt and return the secret stored under *name*.

        Args:
            name: Credential name to look up.

        Returns:
            The decrypted plaintext secret.

        Raises:
            CredentialStoreError: If the name is invalid, the credential does
                not exist, or decryption fails.
        """
        normalized = validate_credential_name(name)
        with self._lock:
            values = self._load_encrypted()
            if normalized not in values:
                raise CredentialStoreError(f"凭据“{normalized}”不存在")
            return self._unprotect(values[normalized])

    def set(self, name: str, secret: str) -> None:
        """Encrypt and store (or overwrite) a secret under *name*.

        Args:
            name: Credential name (validated and normalized).
            secret: Plaintext secret to encrypt and persist.

        Raises:
            CredentialStoreError: If the name is invalid, the store is full
                (MAX_CREDENTIALS), the projected file would exceed
                MAX_CREDENTIAL_FILE_BYTES, or encryption/persistence fails.
        """
        normalized = validate_credential_name(name)
        with self._lock:
            values = self._load_encrypted()
            if normalized not in values and len(values) >= MAX_CREDENTIALS:
                raise CredentialStoreError(f"凭据库最多保存 {MAX_CREDENTIALS} 项")
            values[normalized] = self._protect(str(secret))
            # SAFETY: 写出的一切持久化文件必须能被本版本读回（与 FR-009 的
            # allow_nan=False 同一对称性原则）。读侧以 MAX_CREDENTIAL_FILE_BYTES
            # 为硬上限；写侧若无同等守卫，数百个接近 1MB 的合法秘密会写出超限
            # 文件，下次启动 names/get/set/delete 全部失败——凭据库整体自锁且
            # 程序内无自救路径（FR-RISK-20261010-2501，隔离探针已复现）。仅在
            # 新增内容会超出上限时拒绝；删除路径不经此处，收缩不受影响。
            if self._projected_size(values) > MAX_CREDENTIAL_FILE_BYTES:
                raise CredentialStoreError(
                    "凭据库已满：继续写入将超过 64MB 上限并导致凭据库无法读取。"
                    "请删除部分凭据或缩短秘密内容后重试。"
                )
            self._save_encrypted(values)

    def delete(self, name: str) -> bool:
        """Remove a credential from the store.

        Args:
            name: Credential name to delete.

        Returns:
            True if the credential existed and was removed, False otherwise.

        Raises:
            CredentialStoreError: If the name is invalid or persistence fails.
        """
        normalized = validate_credential_name(name)
        with self._lock:
            values = self._load_encrypted()
            if normalized not in values:
                return False
            del values[normalized]
            self._save_encrypted(values)
            return True
