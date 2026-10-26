# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""引擎性能报告服务。从 RPAEngine 提取的非执行职责。"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class EnginePerformanceMixin:
    """性能指标收集与报告（Mixin，由 RPAEngine 继承）。"""

    def performance_snapshot(self):
        """生成当前性能指标快照。

        Returns:
            性能指标字典；若原生视觉核心可用，附加其统计到 "native" 键。
        """
        report = self.performance.snapshot()
        native_stats = None
        if getattr(self, "native_core", None):
            native_stats = self.native_core.performance_stats()
        if native_stats:
            report["native"] = native_stats
        return report

    def finalize_performance_report(self, outcome):
        """在运行结束时生成并缓存最终性能报告。

        Args:
            outcome: 本次运行结果字符串，如 "finished"、"stopped"、"error"。

        Returns:
            最终性能指标字典，同时保存到 last_performance_report。
        """
        self.performance.set_gauge("run.outcome", str(outcome))
        self.performance.set_gauge(
            "template.cache_bytes", int(getattr(self, "scaled_template_cache_bytes", 0))
        )
        self.last_performance_report = self.performance_snapshot()
        return self.last_performance_report

    def report_status(self, loop_count=1, step=0, total=0, cmd=""):
        """向界面回调上报当前运行进度。

        与上一次完全相同的进度会被跳过，避免重复刷新。

        Args:
            loop_count: 当前循环轮次。
            step: 当前步骤序号。
            total: 步骤总数。
            cmd: 当前正在执行的指令名称。
        """
        payload = {
            "loop": int(loop_count),
            "step": int(step),
            "total": int(total),
            "cmd": str(cmd),
        }
        if payload == self._last_status_payload:
            self.performance.increment("status.duplicate_updates_skipped")
            return
        self._last_status_payload = payload
        started_ns = time.perf_counter_ns()
        if self.callback_status:
            try:
                self.callback_status(payload)
            except Exception as exc:
                logger.warning("状态回调失败: %s", exc, exc_info=True)
        self.performance.increment("status.updates")
        self.performance.observe_since("status.dispatch", started_ns)

    def report_click_indicator(self, x, y, text=""):
        """通知界面在指定坐标显示点击指示器。

        仅当 show_click_indicator 开启且存在回调时才生效。

        Args:
            x: 点击位置横坐标。
            y: 点击位置纵坐标。
            text: 指示器附带的说明文本。
        """
        if not self.show_click_indicator or not self.callback_click_indicator:
            return
        started_ns = time.perf_counter_ns()
        try:
            self.callback_click_indicator(
                {
                    "x": int(round(float(x))),
                    "y": int(round(float(y))),
                    "text": str(text or ""),
                }
            )
        except Exception as exc:
            logger.warning("点击指示器回调失败: %s", exc, exc_info=True)
        finally:
            self.performance.observe_since("indicator.dispatch", started_ns)

    def report_mouse_trail(self, points: object, *, new_path=False):
        """Send one bounded batch of pointer positions to the visual overlay.

        The existing pointer-feedback callback is shared with click indicators
        so both in-process and isolated Runner paths retain one ordered Qt/IPC
        event stream. ``kind`` lets the UI dispatch without creating a second
        callback contract.
        """

        if not self.callback_click_indicator:
            return
        if not isinstance(points, (list, tuple)):
            return
        normalized = []
        for point in points[:16]:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                x, y = point[0], point[1]
                normalized.append(
                    [int(round(float(x))), int(round(float(y)))]
                )
            except (TypeError, ValueError):
                continue
        if not normalized:
            return
        started_ns = time.perf_counter_ns()
        try:
            self.callback_click_indicator(
                {
                    "kind": "mouse_trail",
                    "points": normalized,
                    "new_path": bool(new_path),
                }
            )
        except Exception as exc:
            logger.warning("鼠标移动拖影回调失败: %s", exc, exc_info=True)
        finally:
            self.performance.increment("trail.batches")
            self.performance.increment("trail.points", len(normalized))
            self.performance.observe_since("trail.dispatch", started_ns)
