"""Measure move/resize UI cost using the same single-core CPU convention as the app."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fukua_rpa.ui.controllers.hotkey_controller import HotkeyController  # noqa: E402
from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402

DEFAULT_LIMITS = {
    "move_single_core_percent": 15.0,
    "hover_single_core_percent": 15.0,
    "stable_drag_indicator_single_core_percent": 15.0,
    "manual_drag_single_core_percent": 20.0,
    "resize_single_core_percent": 65.0,
    "settings_resize_single_core_percent": 65.0,
    "materialized_ratio": 0.35,
}


def _run_scenario(app, iterations, callback, pace_seconds):
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for index in range(iterations):
        callback(index)
        app.processEvents()
        if pace_seconds > 0:
            time.sleep(pace_seconds)
    time.sleep(0.18)
    app.processEvents()
    wall_seconds = max(0.000001, time.perf_counter() - wall_started)
    cpu_seconds = max(0.0, time.process_time() - cpu_started)
    return {
        "iterations": iterations,
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "single_core_percent": round(cpu_seconds / wall_seconds * 100.0, 2),
    }


def benchmark(iterations=180, task_count=180, pace_ms=8.0):
    app = QApplication.instance() or QApplication([])
    pace_seconds = max(0.0, float(pace_ms)) / 1000.0
    with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
        HotkeyController, "register_global_hotkeys", lambda _self: True
    ), mock.patch.object(
        QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok
    ), mock.patch.object(
        QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok
    ):
        window = RPAWindow(base_dir=temp_dir)
        window.task_list.clear()
        for index in range(max(0, int(task_count))):
            window.add_row(
                {"type": 1.0, "value": f"{index},{index}"},
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        window.update_indexes()
        window.show()
        app.processEvents()
        window.task_list.schedule_virtual_refresh(immediate=True)
        window.ui_performance.reset()

        viewport = window.task_list.viewport()
        hover_width = max(2, viewport.width() - 2)
        hover_height = max(2, viewport.height() - 2)
        hover = _run_scenario(
            app,
            iterations,
            lambda index: QTest.mouseMove(
                viewport,
                QPoint(
                    1 + (index * 17) % hover_width,
                    1 + (index * 11) % hover_height,
                ),
            ),
            pace_seconds,
        )
        first_rect = window.task_list.visualItemRect(window.task_list.item(0))
        indicator_y = first_rect.bottom() + 1
        window.task_list.drop_indicator.show_at(indicator_y)
        stable_drag_indicator = _run_scenario(
            app,
            iterations,
            lambda _index: window.task_list.drop_indicator.show_at(indicator_y),
            pace_seconds,
        )
        window.task_list.drop_indicator.clear()
        window.task_list.setCurrentRow(3)
        window.task_list.startDrag(Qt.MoveAction)
        # Benchmark scripts intentionally access internals to measure specific behaviors.
        window.task_list._drag_autoscroll_timer.stop()
        drag_height = max(2, viewport.height() - 2)
        manual_drag = _run_scenario(
            app,
            iterations,
            lambda index: window.task_list._update_manual_drag(
                QPoint(
                    viewport.width() // 2,
                    1 + (index * 19) % drag_height,
                )
            ),
            pace_seconds,
        )
        window.task_list.cancel_manual_drag()

        base_x, base_y = window.x(), window.y()
        base_width, base_height = window.width(), window.height()
        move = _run_scenario(
            app,
            iterations,
            lambda index: window.move(
                base_x + index % 17,
                base_y + index % 13,
            ),
            pace_seconds,
        )
        resize = _run_scenario(
            app,
            iterations,
            lambda index: window.resize(
                base_width + index % 120,
                base_height + index % 7,
            ),
            pace_seconds,
        )
        # C-1：设置对话框改为首次打开时构建，`settings_dialog` 在打开前不存在。
        # 走公开入口打开（构建成本落在计时场景之外），随后再读取几何并测缩放。
        window.show_settings_dialog()
        app.processEvents()
        settings_width = window.settings_dialog.width()
        settings_height = window.settings_dialog.height()
        settings_resize = _run_scenario(
            app,
            iterations,
            lambda index: window.settings_dialog.resize(
                settings_width + index % 160,
                settings_height + index % 9,
            ),
            pace_seconds,
        )
        total_rows = window.task_list.count()
        materialized_rows = window.task_list.materialized_row_count()
        report = {
            "schema_version": 3,
            "pace_ms": float(pace_ms),
            "task_count": total_rows,
            "hover": hover,
            "stable_drag_indicator": stable_drag_indicator,
            "manual_drag": manual_drag,
            "move": move,
            "resize": resize,
            "settings_resize": settings_resize,
            "virtualization": {
                "materialized_rows": materialized_rows,
                "materialized_ratio": round(
                    materialized_rows / max(1, total_rows), 4
                ),
            },
            "ui_metrics": window.ui_performance.snapshot(),
            "limits": dict(DEFAULT_LIMITS),
        }
        window.close()
        app.processEvents()
        return report


def assert_limits(report):
    limits = report["limits"]
    failures = []
    if report["hover"]["single_core_percent"] > limits["hover_single_core_percent"]:
        failures.append("blank hover CPU exceeded limit")
    if (
        report["stable_drag_indicator"]["single_core_percent"]
        > limits["stable_drag_indicator_single_core_percent"]
    ):
        failures.append("stable drag indicator CPU exceeded limit")
    if (
        report["manual_drag"]["single_core_percent"]
        > limits["manual_drag_single_core_percent"]
    ):
        failures.append("manual task drag CPU exceeded limit")
    if report["move"]["single_core_percent"] > limits["move_single_core_percent"]:
        failures.append("move CPU exceeded limit")
    if report["resize"]["single_core_percent"] > limits["resize_single_core_percent"]:
        failures.append("resize CPU exceeded limit")
    if (
        report["settings_resize"]["single_core_percent"]
        > limits["settings_resize_single_core_percent"]
    ):
        failures.append("settings resize CPU exceeded limit")
    if (
        report["virtualization"]["materialized_ratio"]
        > limits["materialized_ratio"]
    ):
        failures.append("too many long-list row widgets were materialized")
    if failures:
        raise RuntimeError("; ".join(failures))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=180)
    parser.add_argument("--tasks", type=int, default=180)
    parser.add_argument("--pace-ms", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assert-limits", action="store_true")
    args = parser.parse_args()

    report = benchmark(args.iterations, args.tasks, args.pace_ms)
    if args.assert_limits:
        assert_limits(report)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
