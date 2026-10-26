"""Render deterministic source UI screenshots for layout review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea  # noqa: E402

from fukua_rpa.constants import APP_VERSION  # noqa: E402
from fukua_rpa.ui.accessibility import text_clipping_issues  # noqa: E402
from fukua_rpa.ui.controllers.hotkey_controller import HotkeyController  # noqa: E402
from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog  # noqa: E402


def sampled_color_count(image) -> int:
    colors = set()
    step_x = max(1, image.width() // 24)
    step_y = max(1, image.height() // 18)
    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            colors.add(image.pixelColor(x, y).rgba())
            if len(colors) >= 8:
                return len(colors)
    return len(colors)


def capture(
    app: QApplication,
    widget,
    output: Path,
    *,
    scale,
    theme,
    font_family,
    records,
) -> None:
    widget.show()
    app.processEvents()
    QTest.qWait(80)
    app.processEvents()
    image = widget.grab()
    if image.isNull() or not image.save(str(output), "PNG"):
        raise RuntimeError(f"无法保存界面截图：{output}")
    color_count = sampled_color_count(image.toImage())
    if color_count < 2:
        raise RuntimeError(f"界面截图为空白或近似空白：{output}")
    payload = output.read_bytes()
    records.append(
        {
            "file": output.name,
            "scale": float(scale),
            "theme": str(theme),
            "font_family": str(font_family),
            "width": int(image.width()),
            "height": int(image.height()),
            "sampled_color_count": int(color_count),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "clipping_issues": text_clipping_issues(widget),
        }
    )


def parse_scales(text):
    values = []
    for item in str(text).split(","):
        value = float(item.strip())
        if value not in (1.0, 1.5, 2.0):
            raise argparse.ArgumentTypeError("倍率只允许 1.0、1.5、2.0")
        if value not in values:
            values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("至少选择一个倍率")
    return values


def parse_csv_values(text):
    values = []
    for item in str(text).split(","):
        value = item.strip()
        if value and value not in values:
            values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("至少提供一个值")
    return values


def parse_themes(text):
    values = parse_csv_values(text)
    if any(value not in ("light", "dark") for value in values):
        raise argparse.ArgumentTypeError("主题只允许 light、dark")
    return values


def filename_slug(text):
    clean = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    digest = hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:6]
    return f"{clean or 'font'}-{digest}"


def apply_font_environment(app, family, scale):
    font = QFont(str(family))
    font.setPointSizeF(9.0 * float(scale))
    app.setFont(font)
    escaped = str(family).replace('"', "")
    app.setStyleSheet(
        app.styleSheet() + f'\nQWidget {{ font-family: "{escaped}"; }}'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".scratch" / f"ui-review-{APP_VERSION}",
    )
    parser.add_argument(
        "--themes",
        type=parse_themes,
        default=parse_themes("light,dark"),
        help="逗号分隔的界面主题：light,dark。",
    )
    parser.add_argument(
        "--scales",
        type=parse_scales,
        default=parse_scales("1.0,1.5,2.0"),
        help="逗号分隔的界面倍率：1.0,1.5,2.0。",
    )
    parser.add_argument(
        "--font-families",
        type=parse_csv_values,
        default=parse_csv_values("Microsoft YaHei UI,Segoe UI,SimSun"),
        help="逗号分隔的字体环境。",
    )
    parser.add_argument(
        "--strict-layout",
        action="store_true",
        help="发现明显的单行文字裁切时以非零状态退出。",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    records = []

    with tempfile.TemporaryDirectory() as runtime_dir, mock.patch.object(
        HotkeyController, "register_global_hotkeys", lambda _self: True
    ), mock.patch.object(
        QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok
    ):
        window = RPAWindow(base_dir=runtime_dir)
        window.add_row({"type": 1.0, "value": "320,240", "debug_breakpoint": True})
        window.add_row({"type": 16.0, "value": "counter = 1"})
        window.add_row({"type": 17.0, "value": "counter < 10"})

        sizes = {
            1.0: ((1040, 760), (920, 700), (980, 650)),
            1.5: ((820, 650), (820, 650), (840, 650)),
            2.0: ((900, 700), (900, 700), (900, 700)),
        }
        onboarding_captured = False
        for theme in args.themes:
            window.theme_mode_combo.setCurrentIndex(
                window.theme_mode_combo.findData(theme)
            )
            for font_family in args.font_families:
                font_slug = filename_slug(font_family)
                for scale in args.scales:
                    suffix = int(round(scale * 100))
                    main_size, settings_size, task_size = sizes[scale]
                    window.apply_ui_scale(scale)
                    apply_font_environment(app, font_family, scale)
                    window.resize(*main_size)
                    capture(
                        app,
                        window,
                        output_dir
                        / (
                            f"main-{theme}-{font_slug}-{suffix}-"
                            f"{main_size[0]}x{main_size[1]}.png"
                        ),
                        scale=scale,
                        theme=theme,
                        font_family=font_family,
                        records=records,
                    )

                    window.settings_dialog.resize(*settings_size)
                    window.show_settings_dialog()
                    if not onboarding_captured:
                        capture(
                            app,
                            window.settings_dialog,
                            output_dir
                            / f"settings-onboarding-{theme}-{font_slug}-{suffix}.png",
                            scale=scale,
                            theme=theme,
                            font_family=font_family,
                            records=records,
                        )
                        window.dismiss_fast_response_tip()
                        onboarding_captured = True
                    settings_scroll = window.settings_dialog.findChild(QScrollArea)
                    settings_scroll.verticalScrollBar().setValue(0)
                    capture(
                        app,
                        window.settings_dialog,
                        output_dir
                        / (
                            f"settings-{theme}-{font_slug}-{suffix}-"
                            f"{settings_size[0]}x{settings_size[1]}.png"
                        ),
                        scale=scale,
                        theme=theme,
                        font_family=font_family,
                        records=records,
                    )
                    window.settings_dialog.hide()

                    task_dialog = TaskConfigDialog(
                        window,
                        {
                            "debug_breakpoint": True,
                            "repeat_mode": "重复指定次数",
                            "repeat_count": 3,
                            "coord_step_en": True,
                            "coord_step_direction": "移动到新点位",
                            "coord_step_point": "640,480",
                            "coord_step_max_steps": 6,
                        },
                        image_settings_available=True,
                        point_limit_available=True,
                        coordinate_step_available=True,
                        image_click_point_available=True,
                        base_coordinate=(320, 240),
                        base_dir=runtime_dir,
                    )
                    task_dialog.setAttribute(Qt.WA_DeleteOnClose)
                    task_dialog.resize(*task_size)
                    capture(
                        app,
                        task_dialog,
                        output_dir
                        / (
                            f"step-settings-{theme}-{font_slug}-{suffix}-"
                            f"{task_size[0]}x{task_size[1]}.png"
                        ),
                        scale=scale,
                        theme=theme,
                        font_family=font_family,
                        records=records,
                    )
                    task_dialog.close()
                    app.processEvents()
                    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    app.processEvents()
        window.close()
        app.processEvents()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    report = {
        "version": APP_VERSION,
        "themes": args.themes,
        "font_families": args.font_families,
        "scales": args.scales,
        "screens": [
            {
                "name": screen.name(),
                "geometry": [
                    screen.geometry().x(),
                    screen.geometry().y(),
                    screen.geometry().width(),
                    screen.geometry().height(),
                ],
                "device_pixel_ratio": float(screen.devicePixelRatio()),
            }
            for screen in app.screens()
        ],
        "captures": records,
    }
    report_path = output_dir / "UI_REVIEW.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    clipping = [
        f"{record['file']}: {issue}"
        for record in records
        for issue in record["clipping_issues"]
    ]
    if args.strict_layout and clipping:
        raise RuntimeError("检测到界面文字裁切：\n" + "\n".join(clipping))
    print(f"UI review screenshots: {output_dir}")
    print(f"UI review report: {report_path}")


if __name__ == "__main__":
    main()
