"""Local-time formatting helpers for the scheduled-tasks window Mixins.

Moved out of ``scheduled_tasks_window.py`` when the summary / context-menu
methods were extracted into Mixins, so both can share them without importing
the window module (which would create a cycle).
"""

from __future__ import annotations

from datetime import datetime


def _now_local() -> datetime:
    return datetime.now()


def _fmt_local(dt) -> str:
    if dt is None:
        return "—"
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")
