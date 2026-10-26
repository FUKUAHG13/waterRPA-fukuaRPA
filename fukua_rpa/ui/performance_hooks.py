"""Shared access to the main window's bounded UI performance context."""

from __future__ import annotations


def performance_owner(widget):
    """Return the window that owns interaction metrics for a child widget."""

    try:
        window = widget.window()
        owner_ref = getattr(window, "performance_owner_ref", None)
        owner = owner_ref() if callable(owner_ref) else None
        return owner or window
    except (AttributeError, RuntimeError):
        return None


def ui_metrics(widget):
    owner = performance_owner(widget)
    return getattr(owner, "ui_performance", None) if owner is not None else None


def live_window_interaction(widget):
    owner = performance_owner(widget)
    return bool(getattr(owner, "_live_window_interaction", False))


def schedule_owner_interaction(widget, kind):
    owner = performance_owner(widget)
    schedule = getattr(owner, "_schedule_live_interaction_idle", None)
    if callable(schedule):
        schedule(kind)
