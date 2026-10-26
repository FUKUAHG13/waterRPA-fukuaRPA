import ctypes
import unittest
from unittest import mock

from fukua_rpa.hook_backend import Win32HookBackend


class Win32HookBackendTests(unittest.TestCase):
    def test_installed_callback_is_retained_until_successful_uninstall(self):
        backend = Win32HookBackend()
        callback = object()
        with mock.patch(
            "fukua_rpa.hook_backend.kernel32.GetModuleHandleW", return_value=1
        ), mock.patch(
            "fukua_rpa.hook_backend.user32.SetWindowsHookExW", return_value=123
        ), mock.patch(
            "fukua_rpa.hook_backend.user32.UnhookWindowsHookEx",
            side_effect=(False, True),
        ):
            handle = backend.install_keyboard(callback)
            self.assertIs(backend._installed_callbacks[123], callback)
            self.assertFalse(backend.uninstall(handle))
            self.assertIs(backend._installed_callbacks[123], callback)
            self.assertTrue(backend.uninstall(handle))

        self.assertNotIn(123, backend._installed_callbacks)

    def test_ctypes_handle_is_normalized_for_callback_ownership(self):
        backend = Win32HookBackend()
        callback = object()
        with mock.patch(
            "fukua_rpa.hook_backend.kernel32.GetModuleHandleW", return_value=1
        ), mock.patch(
            "fukua_rpa.hook_backend.user32.SetWindowsHookExW",
            return_value=ctypes.c_void_p(456),
        ), mock.patch(
            "fukua_rpa.hook_backend.user32.UnhookWindowsHookEx", return_value=True
        ):
            handle = backend.install_mouse(callback)
            self.assertIs(backend._installed_callbacks[456], callback)
            self.assertTrue(backend.uninstall(handle))

        self.assertEqual(backend._installed_callbacks, {})


class HookBackendFailureTests(unittest.TestCase):
    """Defensive tests for hook installation failure paths."""

    def test_install_returns_falsy_when_set_hook_fails(self):
        """If SetWindowsHookExW returns 0, install should not register callback."""
        backend = Win32HookBackend()
        with mock.patch(
            "fukua_rpa.hook_backend.kernel32.GetModuleHandleW", return_value=1
        ), mock.patch(
            "fukua_rpa.hook_backend.user32.SetWindowsHookExW", return_value=0
        ):
            result = backend.install_keyboard(lambda *a: None)
            self.assertFalse(result)
            self.assertEqual(backend._installed_callbacks, {})

    def test_uninstall_with_null_handle_returns_true(self):
        """Uninstalling when no hook is active should return True gracefully."""
        backend = Win32HookBackend()
        result = backend.uninstall(None)
        self.assertTrue(result)

    def test_service_snapshot_reports_state(self):
        """service_snapshot should return a dict with expected keys."""
        backend = Win32HookBackend()
        snapshot = backend.service_snapshot()
        self.assertIsInstance(snapshot, dict)
        self.assertIn("service", snapshot)
        self.assertIn("backend", snapshot)
        self.assertIn("available", snapshot)


if __name__ == "__main__":
    unittest.main()
