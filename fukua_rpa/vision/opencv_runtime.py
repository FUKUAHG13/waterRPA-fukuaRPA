# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Bound OpenCV worker threads for predictable portable-runtime CPU usage."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_OPENCV_THREAD_LIMIT = 2


def configure_opencv_threads(
    cv2_module: Any,
    limit: int = DEFAULT_OPENCV_THREAD_LIMIT,
) -> int:
    """Apply a conservative process-wide thread cap and return the actual value."""

    logical_cpus = max(1, int(os.cpu_count() or 1))
    requested = max(1, int(limit))
    threads = min(requested, logical_cpus, DEFAULT_OPENCV_THREAD_LIMIT)
    cv2_module.setNumThreads(threads)
    actual = int(cv2_module.getNumThreads())
    if actual <= 0:
        # Fail-fast: if OpenCV refuses the thread cap, the runtime is misconfigured.
        # This prevents silent performance degradation from unbounded thread pools.
        raise RuntimeError("OpenCV 没有接受线程上限设置")
    return actual
