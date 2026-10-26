"""Client side of the plugin host IPC (design section 5.7).

The engine step calls :meth:`PluginHostClient.call` with a :class:`PluginRequest`
and gets back a :class:`PluginResponse`. In implementation phase P2 the host is a
*fake* in-process host (see :mod:`plugin_host_process`), so the client simply
delegates. The public API is frozen so phase P3 can swap in a real isolated
subprocess speaking the same framed protocol (see :mod:`plugin_protocol`) without
touching the caller in :mod:`engine_actions` / :mod:`plugin_actions`.

This module is Qt-free on purpose: it may be imported from the worker thread.
"""

from __future__ import annotations

from .plugin_host_process import PluginHostProcess
from .plugin_protocol import PluginRequest, PluginResponse


class PluginHostClient:
    """Talks to a plugin host. P2: in-process stub; P3: isolated subprocess."""

    def __init__(self, host: PluginHostProcess | None = None) -> None:
        self._host = host or PluginHostProcess()

    def set_host(self, host: PluginHostProcess) -> None:
        """Replace the host implementation used by subsequent calls.

        Args:
            host: The plugin host that will handle future requests.
        """
        self._host = host

    def call(self, request: PluginRequest, timeout_ms: float = 5000.0) -> PluginResponse:
        """Forward *request* to the host and return its response.

        The stub host is synchronous and bounded, so *timeout_ms* is currently a
        documented ceiling (reserved for the async subprocess transport in P3)
        rather than an enforced deadline.
        """
        return self._host.handle(request)

    def stop(self, step_id: str) -> None:
        """Request cancellation of an in-flight step (P3: forward to host).

        The stub host has no in-flight work, so this is a no-op placeholder that
        keeps the call contract stable for the engine's emergency-stop path.
        """
        return None


_client: PluginHostClient | None = None


def get_plugin_host_client() -> PluginHostClient:
    """Lazily-created process-wide client (the engine step uses this)."""

    global _client
    if _client is None:
        _client = PluginHostClient()
    return _client


def set_plugin_host_client(client: PluginHostClient) -> None:
    """Override the process-wide client singleton (primarily for tests).

    Args:
        client: The client instance that future ``get_plugin_host_client``
            calls will return.
    """
    global _client
    _client = client
