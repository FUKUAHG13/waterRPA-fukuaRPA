"""共享的子进程运行助手（health report §3.5(c)）。

合并 build_release.py / run_impacted_tests.py / verify_release.py 中此前各自
实现的 run() 助手，统一打印、cwd、env、超时与超时后的进程树清理语义，避免漂移。
各调用方通过 on_timeout 保留各自原有的超时异常类型。
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(
    command,
    *,
    timeout: float = 600,
    env=None,
    cwd=None,
    tree_kill: bool = False,
    on_timeout=None,
):
    """在 ROOT（或 cwd）下运行命令，非零退出时抛 ``CalledProcessError``。

    - command: 命令及参数列表。
    - timeout: 超时秒数；超时触发 ``on_timeout``（或默认重抛 ``TimeoutExpired``）。
    - env: 子进程环境变量；``None`` 表示继承当前进程环境。
    - cwd: 工作目录；``None`` 表示仓库根 ``ROOT``。
    - tree_kill: 超时是否用 ``taskkill /T`` 清理整个进程树，避免子进程孤儿持有 DLL。
    - on_timeout: 可选回调 ``on_timeout(cmd, proc, exc)``，在 ``tree_kill``（如启用）
      之后、重抛之前被调用，应自行 ``raise`` 期望的异常类型；省略则重抛原
      ``TimeoutExpired``。
    """
    cmd = [str(value) for value in command]
    print(">", subprocess.list2cmdline(cmd), flush=True)
    work_dir = cwd or ROOT
    proc = subprocess.Popen(command, cwd=work_dir, env=env)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if tree_kill:
            # 先杀进程树，再杀直接子进程。若先 proc.kill()，孙进程可能被 reparent
            # 到其它进程，导致 taskkill /T 按原 PID 找不到它们。
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        proc.kill()  # 确保直接子进程终止
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if on_timeout is not None:
            on_timeout(cmd, proc, exc)
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, command)
