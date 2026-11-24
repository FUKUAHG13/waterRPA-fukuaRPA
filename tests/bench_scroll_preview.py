# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.20 滚动预览本地性能对照（离屏，受控配对）。

固定窗口/数据/滑块序列，输出：
- 拖动期新增 TaskRow 数（应为 0）
- 拖动期每帧绘制的未物化行数（受视口+缓冲限制）
- 滑块移动到对应视口首帧绘制完成的延迟 P95
- 松手补齐延迟
- 整段 CPU 时间（process_time）

该文件不是 pytest 用例；用 python 直接运行，结果写入 JSON 供留证。
"""
import json
import os
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 直接运行脚本时把仓库根加入 sys.path，使 tests 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QStyle,
    QStyleOptionSlider,
    QStyleOptionViewItem,
)

from tests.qt_test_support import create_test_window  # noqa: E402


def _slider_center(sb):
    opt = QStyleOptionSlider()
    sb.initStyleOption(opt)
    handle = sb.style().subControlRect(
        QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, sb
    )
    return handle.center()


def build(n, steps):
    window = create_test_window(steps, tempfile.mkdtemp())
    tl = window.task_list
    tl.clear()
    for i in range(n):
        window.add_row(
            {"type": 1.0, "value": f"{i},{i}", "step_note": "备注说明文本" if i % 7 == 0 else ""},
            record_undo=False, select=False, materialize=False,
            update_indexes=False,
        )
    window.update_indexes()
    window.resize(1100, 760)
    window.show()
    steps.app.processEvents()
    tl.schedule_virtual_refresh(immediate=True)
    steps.app.processEvents()
    return window, tl


def measure(app, n):
    import unittest

    holder = unittest.TestCase("runTest")
    holder.app = app
    window, tl = build(n, holder)
    sb = tl.verticalScrollBar()
    delegate = tl.itemDelegate()
    created = {"n": 0}
    original = window.restore_row_widget

    def counting(item, data):
        created["n"] += 1
        return original(item, data)

    window.restore_row_widget = counting
    center = _slider_center(sb)
    groove = sb.rect()
    top_y = max(groove.top() + 2, center.y())
    bottom_y = min(groove.bottom() - 2, center.y())

    cpu_start = time.process_time()
    latencies = []
    painted_rows = []
    sb.setValue(0)
    app.processEvents()
    QTest.mousePress(sb, Qt.LeftButton, Qt.NoModifier, center)
    app.processEvents()
    values = [sb.maximum(), sb.maximum() // 2, 0, sb.maximum() // 3, sb.maximum()]
    for value in values:
        t0 = time.perf_counter()
        QTest.mouseMove(sb, QPoint(center.x(), bottom_y if value else top_y))
        sb.setValue(value)
        app.processEvents()
        # 主动委托绘制视口内未物化行，模拟一帧预览绘制
        vh = max(0, tl.viewport().height())
        count = 0
        for row, top, bottom in tl._visible_row_geometry():
            if bottom <= 0 or top >= vh:
                continue
            item = tl.item(row)
            index = tl.indexFromItem(item)
            if tl.itemWidget(item) is None and delegate.should_preview(index):
                fields = delegate.preview_fields(index)
                rect = tl.visualRect(index)
                img = QImage(max(1, rect.width()), max(1, rect.height()),
                             QImage.Format_ARGB32)
                painter = QPainter(img)
                opt = QStyleOptionViewItem()
                opt.rect = rect
                opt.palette = tl.palette()
                delegate.draw_preview(painter, opt, fields)
                painter.end()
                count += 1
        latencies.append((time.perf_counter() - t0) * 1000.0)
        painted_rows.append(count)
    during_creates = created["n"]
    release_t0 = time.perf_counter()
    QTest.mouseRelease(sb, Qt.LeftButton, Qt.NoModifier,
                       QPoint(center.x(), bottom_y))
    # 跑事件循环到最终视口补齐
    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline:
        app.processEvents()
        vh = max(0, tl.viewport().height())
        missing = [
            row for row, top, bottom in tl._visible_row_geometry()
            if bottom > 0 and top < vh and tl.itemWidget(tl.item(row)) is None
        ]
        if not missing:
            break
        time.sleep(0.005)
    release_ms = (time.perf_counter() - release_t0) * 1000.0
    cpu_total = time.process_time() - cpu_start
    result = {
        "rows": n,
        "drag_new_taskrows": during_creates,
        "painted_rows_per_frame": painted_rows,
        "max_painted_rows": max(painted_rows) if painted_rows else 0,
        "first_frame_latency_ms_p95": round(
            statistics.quantiles(latencies, n=20)[-1] if len(latencies) >= 20
            else max(latencies), 3
        ),
        "first_frame_latency_ms_max": round(max(latencies), 3),
        "release_fill_ms": round(release_ms, 3),
        "cpu_seconds": round(cpu_total, 4),
        "viewport_height": tl.viewport().height(),
        "buffer_rows": getattr(tl, "VIRTUAL_BUFFER_ROWS", None),
    }
    window.close()
    app.processEvents()
    return result


def main():
    app = QApplication.instance() or QApplication([])
    results = [measure(app, n) for n in (88, 380)]
    report = {
        "note": "v1.1.20 local offscreen scroll preview pairing; same machine, synthetic data",
        "results": results,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "scroll_preview_bench_result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
