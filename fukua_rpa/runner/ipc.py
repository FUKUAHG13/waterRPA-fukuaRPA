"""Local-only named-pipe endpoints for the isolated Runner.

Windows ``AF_PIPE`` traffic never leaves this computer and is process IPC, not
external network access under the product's offline promise. See ADR 0029.
"""

from __future__ import annotations

import os
import re
import secrets

# SAFETY: keep this endpoint authenticated and local-only. Any future transport
# must preserve that boundary; an externally bindable listener would violate it.
RUNNER_IPC_FAMILY = "AF_PIPE"
_PIPE_PREFIX = r"\\.\pipe\fukuaRPA-runner-"
_PIPE_PATTERN = re.compile(r"^\\\\\.\\pipe\\fukuaRPA-runner-[0-9]+-[0-9a-f]{32}$")


def create_runner_pipe_name(*, process_id: int | None = None, token: str | None = None) -> str:
    """Generate a unique, authenticated named-pipe address for a Runner session.

    The pipe name embeds the owning PID and a 128-bit random nonce so that
    only the intended child process can connect.

    Args:
        process_id: PID to embed; defaults to the current process.
        token: Pre-generated 32-hex-char nonce; generated if omitted.

    Returns:
        A fully qualified ``\\\\.\\pipe\\fukuaRPA-runner-<pid>-<nonce>`` address.

    Raises:
        ValueError: If the PID is non-positive or the token is malformed.
    """
    pid = int(os.getpid() if process_id is None else process_id)
    if pid <= 0:
        raise ValueError("Runner 命名管道 PID 无效")
    nonce = str(token or secrets.token_hex(16)).lower()
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("Runner 命名管道随机标识无效")
    return f"{_PIPE_PREFIX}{pid}-{nonce}"


def validate_runner_pipe_name(value: object) -> str:
    """Validate and normalize a Runner named-pipe address.

    Args:
        value: Candidate pipe address (typically from IPC handshake).

    Returns:
        The validated pipe address string.

    Raises:
        ValueError: If the address is too long or does not match the
            expected ``fukuaRPA-runner-<pid>-<nonce>`` pattern.
    """
    address = str(value or "")
    if len(address) > 160 or not _PIPE_PATTERN.fullmatch(address):
        raise ValueError("Runner 命名管道地址无效")
    return address
