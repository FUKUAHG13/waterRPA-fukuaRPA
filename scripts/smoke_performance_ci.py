"""CI 性能冒烟：启动时间 + 5000 步工作流迁移时间。

输出 JSON 格式结果，可与历史基线对比。
不依赖 Qt GUI（纯逻辑路径）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def measure_startup_import():
    """测量核心模块导入时间（代理启动开销）。"""
    start = time.perf_counter()
    import fukua_rpa.config_store  # noqa: F401
    import fukua_rpa.engine  # noqa: F401
    import fukua_rpa.validation  # noqa: F401
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms


def measure_migration_5000_steps():
    """测量 5000 步工作流的 normalize 时间。"""
    from fukua_rpa.task_model import task_data_defaults
    from fukua_rpa.workflow_document import normalize_workflow_tasks

    tasks = [task_data_defaults() for _ in range(5000)]
    start = time.perf_counter()
    normalize_workflow_tasks(tasks)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms


def main():
    results = {
        "core_import_ms": round(measure_startup_import(), 1),
        "normalize_5000_steps_ms": round(measure_migration_5000_steps(), 1),
    }
    print(json.dumps(results, indent=2))

    # 阈值告警（不阻断，仅输出）
    if results["core_import_ms"] > 2000:
        print("WARNING: core import > 2000ms", file=sys.stderr)
    if results["normalize_5000_steps_ms"] > 5000:
        print("WARNING: normalize 5000 steps > 5000ms", file=sys.stderr)


if __name__ == "__main__":
    main()
