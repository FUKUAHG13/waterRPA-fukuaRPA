"""Privacy-safe text helpers shared by logs, diagnostics and workflow review."""

from __future__ import annotations

import html
import logging
import os
import re
import traceback
from collections.abc import Sized
from html.parser import HTMLParser
from types import TracebackType

logger = logging.getLogger(__name__)


MAX_LOG_TEXT_CHARS = 65_536
# Thread safety: set.add() and set() snapshot are atomic under CPython GIL.
# No explicit lock needed unless migrating to free-threaded Python (3.13+).
_PRIVATE_ROOTS: set[str] = set()
_DIRECTIONAL_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}
# User-home-derived roots are stable for the process lifetime, so resolve them
# once at import instead of calling os.environ.get/expanduser on every redaction.
_USER_HOME_ROOTS = frozenset(
    os.path.normcase(os.path.abspath(candidate))
    for candidate in (os.environ.get("USERPROFILE"), os.path.expanduser("~"))
    if candidate
)


class _LegacyMarkupToText(HTMLParser):
    _BREAK_TAGS = {"br", "hr", "p", "div", "pre", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "pre", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def register_private_path_root(path: str | os.PathLike[str]) -> None:
    """Register a directory root whose paths should be redacted from logs.

    Once registered, any absolute path starting with this root will be
    replaced by ``<本机目录>`` in log output produced by :func:`plain_log_text`
    and :func:`redact_sensitive_paths`.

    Args:
        path: A directory path to register. Empty or falsy values are ignored.
    """
    text = os.path.normcase(os.path.abspath(str(path))) if path else ""
    if text:
        _PRIVATE_ROOTS.add(text)


def _remove_control_characters(text: str) -> str:
    result: list[str] = []
    for character in text:
        code = ord(character)
        if character in "\n\t":
            result.append(character)
        elif code < 32 or code == 127:
            result.append(f"\\x{code:02x}")
        elif character in _DIRECTIONAL_CONTROLS:
            result.append(f"\\u{code:04x}")
        else:
            result.append(character)
    return "".join(result)


_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<quote>[\"'])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"']*(?P=quote)"
)
_UNC_PATH_RE = re.compile(
    r"\\\\[^\s\\/:*?\"<>|]+(?:[\\/][^\s\\/:*?\"<>|]+)+"
)
_DRIVE_PATH_RE = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/][^\r\n,，。；;、)\]}>\"'|?*]*"
)


def _redact_absolute_paths(text: str) -> str:
    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{quote}<隐藏路径>{quote}"

    result = _QUOTED_ABSOLUTE_PATH_RE.sub(replace_quoted, text)
    result = _UNC_PATH_RE.sub("<隐藏路径>", result)
    return _DRIVE_PATH_RE.sub("<隐藏路径>", result)


# ---------------------------------------------------------------------------
# 第二道擦除层（P3-4/P3-5）：除路径外，对常见秘密形态也做脱敏。
# 设计基准 #46/#53 要求“秘密值永不进日志/导出文件”，而调用方误把秘密值写进公开
# message 通道时，plain_log_text 当前仅脱敏路径会放行走私。下列正则只针对高置信度
# 的凭据形态，避免误伤普通工作流文本（URL、普通数字/比例等不受影响）。
# ---------------------------------------------------------------------------
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)[\"']?(?P<key>password|passwd|pwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|accesskey|secret[_-]?key|secretkey|private[_-]?key|"
    r"privatekey|authorization|credential|access[_-]?token|secret[_-]?token)"
    r"(?![A-Za-z])(?P<sep>[:=])\s*[\"']?"
    r"(?P<val>[^\s\"']+)"
)
# FR-20260809-D6-③: 值段不再设 512 上限——修复前超长无空格值（如 PEM 私钥、
# 长 API key）只替换前 512 字符，剩余明文残留日志。匹配起点由 key 关键词锚定，
# 值段为互斥字符类线性匹配，无灾难性回溯风险。
# URI schemes are short by specification in practical desktop workflows. Bound
# every component so a long ordinary token cannot make the regex rescan an
# ever-growing suffix from each character position.
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]{0,31}://)"
    r"[^\s:/@]{1,512}:[^\s:/@]{1,512}@"
)
_BEARER_RE = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}")
_JWT_RE = re.compile(r"\b[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")


def _jwt_repl(match: re.Match[str]) -> str:
    """FR-20260812 建议-健壮性: 仅当第一段 base64url 解码为合法 JSON 对象
    时才按 JWT 替换。普通点分文本（hello_world.example_text.test_string）
    首段解码不是 JSON，保留原样，避免误伤。解码失败按非 JWT 处理。
    """
    import base64 as _base64

    token = match.group(0)
    try:
        header = token.split(".", 1)[0]
        padding = "=" * (-len(header) % 4)
        decoded = _base64.urlsafe_b64decode(header + padding)
        text = decoded.decode("utf-8", errors="strict").strip()
    except Exception:  # noqa: BLE001 - 解码失败即非 JWT
        return token
    if text.startswith("{") and text.endswith("}"):
        return "<隐藏令牌>"
    return token
# 独立令牌先按有限长度切分，再在替换函数中判断秘密形态。旧表达式从每个
# 字符位置回溯查找关键词，面对超长普通文本会产生接近二次方的扫描开销。
_STANDALONE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(?P<tok>[A-Za-z0-9_\-]{1,512})(?![A-Za-z0-9_\-])"
)
_SECRET_TOKEN_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "secretkey",
    "privatekey",
)
_SECRET_EXACT_KEYWORDS = {"password", "passwd", "secret", "token", "pwd"}


def _redact_secret_values(text: str) -> str:
    """Erase common secret shapes from *text* (second privacy erasure layer).

    Handles explicit credential contexts (``key=value`` / ``key: value``),
    credentials embedded in URLs (``scheme://user:pass@``), ``Bearer``/``Basic``
    auth headers, JWTs, and standalone tokens containing high-risk keywords
    (e.g. ``my-password-123``). Plain workflow text such as URLs, ratios and
    ordinary numbers is left untouched.
    """
    if not text:
        return text
    # FR-20260811-D3-001: Bearer/Basic 鉴权头必须先于 _SECRET_CONTEXT_RE 处理。
    # 若先跑 _SECRET_CONTEXT_RE，它会以 "authorization" 为 key、只吃掉值段第一个
    # 词 "Bearer"，留下 "Bearer" 之后的 token 明文；随后的 _BEARER_RE 因 "Bearer"
    # 已被替换而无法再命中，最终 "Authorization: <token明文>" 残留日志。先整体
    # 擦除 "Bearer <token>" / "Basic <b64>" 形态，再由上下文规则处理其余 key=value。
    result = _BEARER_RE.sub("<隐藏鉴权头>", text)
    result = _SECRET_CONTEXT_RE.sub(_context_repl, result)
    result = _URL_CREDENTIAL_RE.sub(r"\1<隐藏凭据>@", result)
    # FR-20260812 建议-健壮性: JWT 替换带结构验证（首段 JSON），避免误伤
    # hello_world.example_text.test_string 等普通点分文本。
    result = _JWT_RE.sub(_jwt_repl, result)

    def _standalone_repl(match: re.Match[str]) -> str:
        token = match.group("tok")
        lowered = token.lower()
        if not any(keyword in lowered for keyword in _SECRET_TOKEN_KEYWORDS):
            return token
        if lowered in _SECRET_EXACT_KEYWORDS:
            return token
        # 字段名和人工标签经常包含 token/secret（例如
        # secret_text_action_contents_logged、DIRECTORY_USER_TOKEN）。只有带数字，
        # 或足够长且大小写混合的令牌才按独立秘密值处理。显式 key=value、
        # Authorization、URL 凭据和 JWT 已由上方更强规则无条件处理。
        has_digit = any(ch.isdigit() for ch in token)
        mixed_case = any(ch.islower() for ch in token) and any(
            ch.isupper() for ch in token
        )
        if has_digit or (len(token) >= 24 and mixed_case):
            return "<隐藏秘密值>"
        return token

    result = _STANDALONE_TOKEN_RE.sub(_standalone_repl, result)
    return result


# 各替换规则输出的占位符全集。FR-20260811-D3-001: _BEARER_RE 先于
# _SECRET_CONTEXT_RE 执行后，Authorization 的值段可能已是占位符；_context_repl
# 只对**已知占位符**原样保留，避免把形如 <token-abc123> 的真实尖括号值误放行。
_REDACTION_PLACEHOLDERS = frozenset(
    {
        "<隐藏鉴权头>",
        "<隐藏凭据>",
        "<隐藏令牌>",
        "<隐藏秘密值>",
        "<隐藏路径>",
        "<本机目录>",
    }
)


def _context_repl(match: re.Match[str]) -> str:
    value = match.group("val")
    if value in _REDACTION_PLACEHOLDERS:
        return f"{match.group('key')}{match.group('sep')}{value}"
    return f"{match.group('key')}{match.group('sep')}<隐藏秘密值>"


def redact_secret_values(text: str) -> str:
    """Public wrapper: erase common secret shapes from *text*.

    Use at trust boundaries alongside :func:`redact_sensitive_paths` so
    credential values never leak into logs or stored artifacts (see P3-4/P3-5).
    """

    if not text:
        return text
    return _redact_secret_values(text)


# Thread safety: keys come from a fixed set of registered roots (bounded).
# Under CPython GIL, dict single-key read/write is atomic. No explicit lock
# needed unless migrating to free-threaded Python (3.13+).
_compiled_patterns: dict[str, re.Pattern[str]] = {}


def _get_escaped_pattern(root: str) -> re.Pattern[str]:
    if root not in _compiled_patterns:
        # DESIGN: match root + trailing path segments so that user subdirectory
        # structure is fully consumed by the <本机目录> replacement, not left
        # as a bare relative tail that _DRIVE_PATH_RE cannot catch (no drive).
        # Spaces are allowed inside segments (e.g. "My Documents"); the match
        # stops at quotes, angle brackets, pipes, or newlines which delimit
        # paths in log output.
        _compiled_patterns[root] = re.compile(
            re.escape(root) + r"(?:[\\/][^\"'<>|\r\n]*)*",
            re.IGNORECASE,
        )
    return _compiled_patterns[root]


def _redact_registered_paths(text: str) -> str:
    result = text
    roots = set(_PRIVATE_ROOTS)
    roots.update(_USER_HOME_ROOTS)
    for root in sorted(roots, key=len, reverse=True):
        variants = {root, root.replace("\\", "/")}
        for variant in variants:
            result = _get_escaped_pattern(variant).sub("<本机目录>", result)
    return _redact_absolute_paths(result)


def redact_sensitive_paths(text: str) -> str:
    """Redact absolute and registered local paths from *text*.

    Public wrapper around the private registered-path redactor. Use it at trust
    boundaries (e.g. before persisting plugin error/result text) so user file
    paths never leak into logs or stored artifacts (see review item #11 / #53).
    """

    if not text:
        return text
    return _redact_registered_paths(text)


def plain_log_text(
    value: object,
    *,
    max_chars: int = MAX_LOG_TEXT_CHARS,
    redact_paths: bool = True,
) -> str:
    """Convert legacy markup to bounded plain text, optionally hiding local paths.

    Strips HTML-like tags (used by legacy workflow step descriptions), removes
    control characters, redacts registered and absolute filesystem paths, and
    truncates the result to *max_chars*.

    Args:
        value: Arbitrary value to render as log-safe text.
        max_chars: Maximum output length (minimum 256, default 65536).
        redact_paths: Whether to replace local paths with placeholders.

    Returns:
        A sanitized, bounded plain-text string suitable for log output.
    """

    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if "<" in raw and ">" in raw:
        parser = _LegacyMarkupToText()
        try:
            parser.feed(raw)
            parser.close()
            raw = "".join(parser.parts)
        except Exception as exc:
            logger.warning("HTML 解析失败，回退到正则清理: %s", exc, exc_info=True)
            raw = re.sub(r"<[^>]*>", "", raw)
            raw = html.unescape(raw)
    raw = _remove_control_characters(raw)
    if redact_paths:
        raw = _redact_registered_paths(raw)
    # P3-4/P3-5: 第二道擦除层——无论 redact_paths 开关，秘密值形态都须擦除
    # （设计基准 #46/#53：秘密值永不进日志，不受本地调试关闭路径脱敏影响）。
    raw = _redact_secret_values(raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip("\n")
    limit = max(256, int(max_chars))
    if len(raw) > limit:
        raw = raw[:limit] + f"\n……日志内容过长，已省略 {len(raw) - limit} 个字符"
    return raw


def value_metadata(value: object) -> str:
    """Return a privacy-safe type/length summary for a value without revealing content.

    Useful for logging variable metadata when privacy protection is active and
    the actual value must not appear in logs.

    Args:
        value: Any Python object.

    Returns:
        A string like ``"类型 str，长度 42"`` describing type and optional length.
    """
    type_name = type(value).__name__
    if isinstance(value, Sized):
        length: int | None = len(value)
    else:
        length = None
    return f"类型 {type_name}" + (f"，长度 {length}" if length is not None else "")


def sensitive_text_metadata(value: object) -> str:
    """Return only the character count of a sensitive value, never its content.

    Designed for logging secret or credential fields where even a truncated
    preview would be a privacy violation.

    Args:
        value: The sensitive value (only its string length is reported).

    Returns:
        A string like ``"12 个字符"`` indicating length only.
    """
    return f"{len(str(value or ''))} 个字符"


def private_debug_value(value: object, *, max_chars: int = 4096) -> str:
    """Return a bounded repr for an explicitly unprotected debug log field.

    SAFETY: Only call this when the user has disabled privacy protection on
    their machine. Credential values and secret-text action content must never
    pass through this function regardless of the privacy toggle.

    Args:
        value: The object to represent.
        max_chars: Maximum output length (minimum 128, default 4096).

    Returns:
        A possibly-truncated repr string.
    """

    try:
        text = repr(value)
    except Exception as exc:
        logger.warning("对象 repr 失败: %s", exc, exc_info=True)
        text = f"<{type(value).__name__} repr failed>"
    limit = max(128, int(max_chars))
    if len(text) > limit:
        text = text[:limit] + f"…（另有 {len(text) - limit} 个字符）"
    return text


def safe_error_name(error: BaseException | None) -> str:
    """Return the class name of an exception for safe display in logs.

    Args:
        error: An exception instance, or None.

    Returns:
        The exception type name, or ``"UnknownError"`` if *error* is None.
    """
    return type(error).__name__ if error is not None else "UnknownError"


def private_error_text(error: BaseException | None) -> str:
    """Return bounded exception details for logs after an explicit privacy opt-out.

    SAFETY: Only call this when the user has disabled privacy protection.
    Includes the exception class name and a bounded repr of the message.

    Args:
        error: An exception instance, or None.

    Returns:
        A string like ``"ValueError: <bounded message>"``, or
        ``"UnknownError"`` if *error* is None.
    """

    if error is None:
        return "UnknownError"
    return f"{type(error).__name__}: {private_debug_value(str(error), max_chars=4096)}"


def safe_exception_report(
    exception_type: type[BaseException] | None,
    traceback_object: TracebackType | None,
) -> str:
    """Build a privacy-safe crash summary with type name and recent frames.

    Only file basenames (not full paths) and line numbers are included, so the
    report can be attached to diagnostics without leaking directory structure.

    Args:
        exception_type: The exception class (or None).
        traceback_object: A traceback object from ``sys.exc_info()``, or None.

    Returns:
        A multi-line string listing the exception type and up to 20 recent
        stack frames (basename:line in function).
    """
    name = getattr(exception_type, "__name__", str(exception_type or "Exception"))
    frames: list[traceback.FrameSummary] = (
        traceback.extract_tb(traceback_object) if traceback_object else []
    )
    lines = [f"异常类型：{name}"]
    for frame in frames[-20:]:
        lines.append(
            f"  {os.path.basename(frame.filename)}:{int(frame.lineno or 0)} in {frame.name}"
        )
    return "\n".join(lines)
