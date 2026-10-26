"""Versioned, path-free contracts for replaceable runtime backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

SERVICE_CATALOG_VERSION = 1
SCREENSHOT_SERVICE_CONTRACT_VERSION = 1
VISION_SERVICE_CONTRACT_VERSION = 1
INPUT_SERVICE_CONTRACT_VERSION = 1
HOOK_SERVICE_CONTRACT_VERSION = 1

_CANCELLATION_MODES = {"none", "bounded_call", "cooperative", "process"}


@dataclass(frozen=True)
class BackendServiceDescriptor:
    """Immutable description of one runtime backend and its capabilities.

    Instances are converted to plain dicts via ``snapshot()`` for inclusion
    in the service catalog exposed to diagnostics and the UI.
    """

    service: str
    contract_version: int
    backend: str
    requested: bool
    available: bool
    native: bool
    capabilities: tuple[str, ...]
    cancellation: str
    timeout_ms: int = 0
    fallback_name: str = ""
    fallback_available: bool = False

    def snapshot(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the runtime service catalog.

        Returns:
            A dict with sorted capabilities and validated cancellation mode.

        Raises:
            ValueError: When ``cancellation`` is not a recognized mode.
        """
        cancellation = str(self.cancellation)
        if cancellation not in _CANCELLATION_MODES:
            raise ValueError(f"未知服务取消模式：{cancellation}")
        result: dict[str, Any] = {
            "service": str(self.service),
            "contract_version": int(self.contract_version),
            "backend": str(self.backend),
            "requested": bool(self.requested),
            "available": bool(self.available),
            "native": bool(self.native),
            "capabilities": sorted({str(item) for item in self.capabilities}),
            "cancellation": cancellation,
            "timeout_ms": max(0, int(self.timeout_ms)),
        }
        if self.fallback_name:
            result["fallback"] = {
                "name": str(self.fallback_name),
                "available": bool(self.fallback_available),
            }
        return result


@runtime_checkable
class ScreenshotService(Protocol):
    """Structural contract for screen-capture backends."""

    def virtual_screen_rect(self) -> Any: ...

    def capture_rgb(self, region: Any) -> Any: ...

    def screenshot_service_snapshot(self, *, requested: bool = True, fallback_available: bool = True) -> dict[str, Any]: ...


@runtime_checkable
class VisionService(Protocol):
    """Structural contract for template-matching backends."""

    def find_template(self, *args: Any, **kwargs: Any) -> Any: ...

    def cancel_active_search(self) -> None: ...

    def service_snapshot(self, *, requested: bool = True, fallback_available: bool = True) -> dict[str, Any]: ...


@runtime_checkable
class InputInjectionService(Protocol):
    """Structural contract for mouse/keyboard injection backends."""

    def move_to(self, x: int, y: int, *, duration: float = 0) -> None: ...

    def position(self) -> Any: ...

    def mouse_down(self, *, button: str) -> None: ...

    def mouse_up(self, *, button: str) -> None: ...

    def scroll(self, amount: int) -> None: ...

    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def service_snapshot(self) -> dict[str, Any]: ...


@runtime_checkable
class HookService(Protocol):
    """Structural contract for low-level hook backends."""

    def install_keyboard(self, callback: Any) -> Any: ...

    def install_mouse(self, callback: Any) -> Any: ...

    def uninstall(self, handle: Any) -> bool: ...

    def service_snapshot(self) -> dict[str, Any]: ...


def make_service_catalog(*snapshots: dict[str, Any]) -> dict[str, Any]:
    """Combine individual service snapshots into a versioned catalog dict.

    Args:
        *snapshots: One or more dicts produced by ``BackendServiceDescriptor.snapshot()``.

    Returns:
        A dict with ``catalog_version`` and a sorted ``services`` list.
    """
    services = sorted(
        (dict(item) for item in snapshots),
        key=lambda item: str(item.get("service", "")),
    )
    return {
        "catalog_version": SERVICE_CATALOG_VERSION,
        "services": services,
    }


def build_runtime_service_catalog(engine: Any) -> dict[str, Any]:
    """Describe active and fallback backends without paths or user content."""

    core = getattr(engine, "native_core", None)
    native_requested = bool(getattr(engine, "use_native_core", False))
    opencv_available = bool(getattr(engine, "opencv_available", False))
    if core is not None:
        vision = core.service_snapshot(
            requested=native_requested,
            fallback_available=opencv_available,
        )
        screenshot = core.screenshot_service_snapshot(
            requested=native_requested,
            fallback_available=True,
        )
    else:
        vision = BackendServiceDescriptor(
            service="vision",
            contract_version=VISION_SERVICE_CONTRACT_VERSION,
            backend="unavailable",
            requested=native_requested,
            available=False,
            native=True,
            capabilities=(),
            cancellation="none",
            fallback_name="opencv",
            fallback_available=opencv_available,
        ).snapshot()
        screenshot = BackendServiceDescriptor(
            service="screenshot",
            contract_version=SCREENSHOT_SERVICE_CONTRACT_VERSION,
            backend="unavailable",
            requested=native_requested,
            available=False,
            native=True,
            capabilities=(),
            cancellation="none",
            fallback_name="mss_pyautogui",
            fallback_available=True,
        ).snapshot()
    input_backend = getattr(engine, "input_backend", None)
    if input_backend is not None and hasattr(input_backend, "service_snapshot"):
        input_service = input_backend.service_snapshot()
    else:
        input_service = BackendServiceDescriptor(
            service="input",
            contract_version=INPUT_SERVICE_CONTRACT_VERSION,
            backend="unavailable",
            requested=True,
            available=False,
            native=False,
            capabilities=(),
            cancellation="none",
        ).snapshot()

    from .hook_backend import win32_hook_backend

    hook = win32_hook_backend.service_snapshot(
        requested=bool(getattr(win32_hook_backend, "has_active_hooks", False))
    )
    return make_service_catalog(vision, screenshot, input_service, hook)
