"""Shared environment replacement helper for tests.

Use with ``mock.patch.object(os, "environ", host_environ(...))``.

DESIGN: 禁止改回「对 ``os.environ`` 使用字典式环境补丁」（即把
``mock.patch.dict`` 的第一个参数设为真实进程环境）的写法。该写法的
``_patch_dict`` 以 ``in_dict.copy()`` 全量快照宿主环境，退出时
``_unpatch_dict`` 先 ``_clear_dict(in_dict)`` 再 ``in_dict.update(original)``；
Windows 上单个环境变量超过 32767 字符时 ``os.environ[key] = value`` 赋值会抛
``ValueError: the environment variable is longer than 32767 characters``，
还原中途中断，PATH 等键永久丢失，其后所有派生子进程的用例级联失败。
先例与逐字机制见 ``tests/test_runner_process_coverage.py:1170-1180``
（FR-20260923-003），本轮复发与修复见 REVIEW-20261108-001 的 ``FR-001``。
正确写法是替换 ``os.environ`` 对象引用（``mock.patch.object``），见本模块
``host_environ``。

SAFETY: 本函数返回宿主环境的**普通 dict 副本**，不剔除任何键、不回写真实
``os.environ``。``mock.patch.object`` 只替换对象引用，退出时恢复引用而不重放
宿主环境，因此不受环境块长度影响。保留 PATH 等父环境其余变量是项目既有断言
（``tests/test_pyright_plugin_baseline.py:224`` 的 ``assertIn("PATH", env)``）
与真实 pyright 子进程的要求，不可换成空 dict。

依据（B 实测，REVIEW-20261108-001 / 04_B_方案审核.md §C.2）：显式
``subprocess.run(env=...)`` 传入含 349469 字符变量的完整环境时子进程可正常启动
（rc=0）；32767 字符限制只作用于 ``os.environ[key] = value`` 赋值路径，普通 dict
不经该路径。因此这里不做超长键剔除——它是防御性措施，未被证实为必需，且会静默
改变被测代码看到的环境。
"""

from __future__ import annotations

import os


def host_environ(**overrides: str) -> dict[str, str]:
    """Return a mutable copy of the host environment with ``overrides`` applied."""

    env = dict(os.environ)
    env.update(overrides)
    return env
