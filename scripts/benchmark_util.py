"""Shared utilities for benchmark scripts."""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes

# --- CPU core-class affinity for the performance gate (OPEN-RC-20261015-PERF) -------
#
# 背景：宿主为混合核心（P-class / E-class）CPU 时，基准进程未绑定核心会让同一指标
# 因调度落点产生约 2× 的波动。本模块提供「运行时推导目标核心类别 → 绑定 → 读回自检」
# 的**单一实现**，供各基准脚本入口调用。产品运行时代码零改动，全局电源设置不改。
#
# 掩码**不得硬编码**：目标类别取机器自报 ``EfficiencyClass`` 的最大者（Windows 约定
# 类别值越高越高性能），掩码由该类全部逻辑处理器按位或得到。本机推导结果为
# ``target_class=1``、``mask=0xFFFF``，但那是**推导结果**，不是来源。
#
# 记录布局（x64，以记录自报 ``Size``=32 为步长）：
#   Size@0 Type@4 | Id@8 Group@12 LPI@14 CoreIndex@15 LLC@16 Numa@17
#   EfficiencyClass@18 AllFlags@19 SchedulingClass@20 AllocationTag@24
#
# ``all_flags`` 只按**原始字节**记录：其位级语义（Parked/Allocated/…）按文档标注，
# 但未在本环境独立验证，且连续两次查询取值不同（动态电源/调度状态）。
# **本批次任何决策都不依赖该字段。**

_OFF_SIZE = 0
_OFF_LPI = 14
_OFF_CORE_INDEX = 15
_OFF_EFFICIENCY_CLASS = 18
_OFF_ALL_FLAGS = 19
_OFF_SCHEDULING_CLASS = 20


def percentile(values, percent):
    """Return the given percentile of a list of values using nearest-rank method.

    DESIGN (SCR03): 采用 nearest-rank 法（n 样本按排序后取第 ceil(n*p/100)-1 位）。
    当样本量 n 较小时，P95 会**退化为接近甚至等于 max**（数学可证：n=10 时
    ceil(10*0.95)-1 = 9 即末位 = max；n=20 时 index=18，P95 < max）。
    这是**有意的**保守取舍：门禁脚本（benchmark_vision.py 的 --assert-limits、
    benchmark_native_scheduler.py 的基准报告）用 P95 与阈值比较，小样本下
    P95≈max 使门禁更严格（更可能拦截回归），方向安全。若未来需要更稳健的
    P95，应改用线性插值法并同步调整门禁阈值——不要只改这里而不同步阈值。
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile called with empty data")
    index = max(0, math.ceil(len(ordered) * percent / 100.0) - 1)
    return ordered[min(index, len(ordered) - 1)]


def read_cpu_set_map():
    """Return one dict per logical processor from ``GetSystemCpuSetInformation``.

    步长取自记录自报的 ``Size`` 字段，字段偏移按文档布局（见模块 docstring）。
    该调用不可用时**显式失败**，不猜测类别。
    """
    kernel32 = ctypes.windll.kernel32
    func = kernel32.GetSystemCpuSetInformation
    func.restype = ctypes.c_int
    func.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]

    buffer = ctypes.create_string_buffer(4096)
    needed = ctypes.c_uint32(0)
    func(buffer, ctypes.c_uint32(len(buffer)), ctypes.byref(needed), None, 0)
    total = needed.value
    if not total:
        raise RuntimeError("GetSystemCpuSetInformation returned no data")

    stride = int.from_bytes(buffer.raw[_OFF_SIZE : _OFF_SIZE + 4], "little")
    if stride <= 0 or total % stride:
        raise RuntimeError(f"unexpected CPU set record size {stride} for {total} bytes")

    rows = []
    for index in range(total // stride):
        raw = buffer.raw[index * stride : (index + 1) * stride]
        rows.append(
            {
                "record": index,
                "size_field": stride,
                "lpi": raw[_OFF_LPI],
                "core_index": raw[_OFF_CORE_INDEX],
                "efficiency_class": raw[_OFF_EFFICIENCY_CLASS],
                "all_flags": raw[_OFF_ALL_FLAGS],
                "scheduling_class": int.from_bytes(
                    raw[_OFF_SCHEDULING_CLASS : _OFF_SCHEDULING_CLASS + 4], "little"
                ),
            }
        )
    rows.sort(key=lambda row: row["lpi"])
    return rows


def derive_target_core_class(rows):
    """Return ``(target_class, mask, lpis)`` for the most performant reported class.

    Windows 约定 ``EfficiencyClass`` 越大越高性能，故目标类别取最大值；掩码由该类
    全部逻辑处理器按位或得到。纯函数，便于单测覆盖推导逻辑。
    """
    if not rows:
        raise RuntimeError("derive_target_core_class called with no CPU set rows")
    target_class = max(row["efficiency_class"] for row in rows)
    lpis = [row["lpi"] for row in rows if row["efficiency_class"] == target_class]
    mask = 0
    for lpi in lpis:
        mask |= 1 << lpi
    return target_class, mask, lpis


def verify_affinity_readback(expected_mask, actual_mask, target_lp_count):
    """Self-check the affinity read-back; raise unless it matches the derived target.

    任何不一致都必须**显式中止**基准，绝不静默退化到未绑定运行
    （见 ``02_PERF修正方案-OptionA.md`` §4.3）。返回实际可用逻辑处理器数。
    """
    if actual_mask != expected_mask:
        raise RuntimeError(
            f"affinity self-check failed: expected {expected_mask:#x}, read back {actual_mask:#x}"
        )
    usable = bin(actual_mask).count("1")
    if usable != target_lp_count:
        raise RuntimeError(
            f"affinity self-check failed: {usable} usable LP != {target_lp_count} target-class LPs"
        )
    return usable


def _sample_cpu_load_percent(seconds):
    """Return a short-window average CPU load sample, or ``None`` if psutil is absent."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return None
    return round(float(psutil.cpu_percent(interval=seconds)), 1)


def apply_benchmark_affinity(load_sample_seconds=0.5):
    """Bind this benchmark process to the machine-reported top core class.

    推导目标类别 → 设置进程亲和掩码 → 读回自检 → 记录环境字段。任何一步失败都抛
    ``RuntimeError``（基准中止），**不**回退到未绑定运行。返回可直接并入基准报告的字段：

    ``cpu_core_class`` / ``affinity_mask_hex`` / ``affinity_verified`` /
    ``usable_lp_count`` / ``usable_lp_count_before`` / ``load_sample_percent``

    （前五项对应方案 §3.3 的「新增字段」；``usable_lp_count_before`` 是为落实 §3.3 表格
    要求的「绑定前/后对比」而增加的只增字段。）
    """
    rows = read_cpu_set_map()
    target_class, mask, lpis = derive_target_core_class(rows)

    kernel32 = ctypes.windll.kernel32
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    kernel32.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetProcessAffinityMask.restype = wintypes.BOOL

    handle = kernel32.GetCurrentProcess()
    process_before = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    if not kernel32.GetProcessAffinityMask(
        handle, ctypes.byref(process_before), ctypes.byref(system_mask)
    ):
        raise RuntimeError("GetProcessAffinityMask failed before binding")
    if mask & ~system_mask.value:
        raise RuntimeError(
            f"target mask {mask:#x} is not a subset of the system mask {system_mask.value:#x}"
        )
    if not kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(mask)):
        raise RuntimeError(f"SetProcessAffinityMask({mask:#x}) failed")

    process_after = ctypes.c_size_t()
    if not kernel32.GetProcessAffinityMask(
        handle, ctypes.byref(process_after), ctypes.byref(system_mask)
    ):
        raise RuntimeError("GetProcessAffinityMask failed after binding")
    usable = verify_affinity_readback(mask, process_after.value, len(lpis))

    return {
        "cpu_core_class": target_class,
        "affinity_mask_hex": hex(mask),
        "affinity_verified": True,
        "usable_lp_count": usable,
        "usable_lp_count_before": bin(process_before.value).count("1"),
        "load_sample_percent": _sample_cpu_load_percent(load_sample_seconds),
    }
