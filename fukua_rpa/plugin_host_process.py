"""Plugin host process (design section 5.7).

Code plugins run in an isolated host process, never inside the main UI process.
This module is the PLUGIN-SIDE runtime: it receives a :class:`PluginRequest`
over the versioned IPC, validates the call against the installed-plugin registry,
and returns a :class:`PluginResponse`.

STATUS (implementation phase P2 — see docs/adr/0036-plugin-system.md; the
original implementation plan §8 lives in an untracked local document):
A real WASM/WASI sandbox is a later, separate phase (P3). Until that runtime is
chosen and frozen, this module is a *fake host* that verifies the complete call
chain (request -> validation -> response) without executing third-party code.
It intentionally provides NO network, filesystem, or arbitrary-code path, so the
offline-only product boundary (design §3.3) is preserved.

This module is DATA-ONLY with respect to Qt: it imports neither PySide nor any
fukua_rpa runtime module, so it can later be moved verbatim into a subprocess.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .plugin_protocol import PluginRequest, PluginResponse
from .plugin_registry import PluginRegistry, PluginStatus

logger = logging.getLogger(__name__)


class PluginHostProcess:
    """Fake host that validates and acks plugin-action requests (P2 stub).

    The host is constructed with the app-level :class:`PluginRegistry` so it can
    check that the referenced plugin is installed, compatible, enabled, and that
    the requested action exists. It does NOT execute arbitrary code.
    """

    def __init__(self, registry: PluginRegistry | None = None) -> None:
        self._registry = registry

    def set_registry(self, registry: PluginRegistry) -> None:
        """Install the registry used to validate subsequent requests.

        Args:
            registry: The application-level plugin registry to consult.
        """
        self._registry = registry

    # -- request handling ---------------------------------------------------

    def handle(self, request: PluginRequest) -> PluginResponse:
        """Validate *request* and return a response.

        Never raises for a malformed *request* — malformed input is reported as
        a failure response so the caller (engine step) can fail the run cleanly.
        """
        start_ns = time.monotonic_ns()
        ok, result, error = self._try_handle(request)
        elapsed_ms = (time.monotonic_ns() - start_ns) / 1_000_000.0
        if not ok:
            return PluginResponse(
                run_id=request.run_id,
                step_id=request.step_id,
                ok=False,
                result=None,
                error=error,
            )
        if not isinstance(result, dict):  # noqa: E721
            result = {"result": result}
        result.setdefault("_host", "stub")  # pyright: ignore[reportArgumentType, reportCallIssue]
        result.setdefault("_elapsed_ms", round(elapsed_ms, 3))  # pyright: ignore[reportArgumentType, reportCallIssue]
        return PluginResponse(
            run_id=request.run_id,
            step_id=request.step_id,
            ok=True,
            result=result,
            error=None,
        )

    def _try_handle(
        self, request: PluginRequest
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """Validate and produce the stub result for *request*.

        Returns:
            A ``(ok, result, error)`` tuple. On success ``ok`` is True and
            ``result`` holds the echo payload; on failure ``ok`` is False and
            ``error`` holds a human-readable reason.
        """
        registry = self._registry
        if registry is None:
            return False, None, "插件注册表不可用"

        plugin_id = request.plugin_id
        action_id = request.action_id
        try:
            status = registry.status(plugin_id)
        except Exception as exc:
            logger.warning("查询插件状态失败 (%s): %s", plugin_id, exc, exc_info=True)
            return False, None, f"未知插件：{plugin_id}"

        if status == PluginStatus.CORRUPT:
            return False, None, f"插件已损坏：{plugin_id}"
        if status == PluginStatus.INCOMPATIBLE:
            return False, None, f"插件不兼容当前版本：{plugin_id}"
        if status == PluginStatus.DEPENDENCY_MISSING:
            return False, None, f"插件依赖缺失：{plugin_id}"
        if status != PluginStatus.ENABLED:
            return False, None, f"插件未启用：{plugin_id}"

        action = registry.get_action(plugin_id, action_id)
        if action is None:
            return False, None, f"插件动作不存在：{plugin_id}/{action_id}"

        # P2: no executable code path. The complete call chain is verified and a
        # benign stub result is returned. Real execution moves to the WASM host
        # in phase P3. We still echo the action's declared output shape so the
        # caller can confirm the contract.
        return (
            True,
            {
                "plugin_id": plugin_id,
                "action_id": action_id,
                "action_name": getattr(action, "name", action_id),
                "echo": _safe_echo(request.payload),
            },
            None,
        )


def _safe_echo(payload: Any) -> dict[str, str] | None:
    """Return a copy-safe preview of the request payload (no secrets/large data)."""

    if not isinstance(payload, dict):
        return None
    preview: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value)
            preview[key] = text[:64] + ("…" if len(text) > 64 else "")
        else:
            preview[key] = f"<{type(value).__name__}>"
    return preview
