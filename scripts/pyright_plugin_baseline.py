"""FR-015 插件数据层 pyright 存量基线（棘轮）。

维护者 2026-09-20 裁定采用"基线棘轮"策略（见 `REVIEW-20260916-001/10_返修复验_01.md` §10.2）：

1. 把宿主侧插件家族的既有 strict 诊断固化为**临时存量基线**，仅豁免这些已确认条目；
2. **新增插件模块必须严格通过**（不得进入基线）；
3. **现有模块不得新增或扩大报错**；
4. 修改相关代码时应**同步减少**其命中的存量项；
5. 基线**只能递减、不得增加**；
6. 待清零后**删除豁免**，并把相关模块纳入 `pyrightconfig.strict.json` 的严格阻断门禁。

范围沿革：最初仅 `plugin_manifest.py` / `plugin_package.py` / `plugin_protocol.py`（63 条）；
2026-09-20 维护者裁定 **D-9 采纳 (a)**，扩展至整个 `fukua_rpa/plugin_*.py` 家族（**145 条**）。

设计要点：

* 比对键为「**文件路径 + 错误代码 + 符号/摘要（message）**」的**多重集**，因此行号漂移不会
  误判为"新增报错"；`line` / `column` 仅作人工定位信息，不参与比对。
* `pyrightconfig.strict.json` 的 `include` 不含这三个模块，故棘轮使用独立配置
  `pyrightconfig.plugin_baseline.json`；本策略**不改变**既有 8 个严格模块的覆盖，
  也**不降低**正式发布门禁（`verify_release.py` 仍用 strict 配置）。
* 本模块只做静态检查工具链工作，不涉及运行时代码、用户可见行为或配置行为。

用法：

```text
python scripts/pyright_plugin_baseline.py            # 校验当前状态与基线是否一致（不一致退出码 1）
python scripts/pyright_plugin_baseline.py --write    # 用当前状态重写基线（拒绝增长与异常缩减，除非 --force）
```

写盘守卫与只读校验共用同一套判定：`--write` 会先比对当前状态与既有基线，拒绝任何
"新增/扩大"以及"异常大幅缩减"（疑似路径归一化或工具链故障）；只有基线文件尚不存在
（首次初始化）或显式 `--force` 时才绕过（见 `FR-20260920-001`）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLUGIN_PYRIGHT_CONFIG = "pyrightconfig.plugin_baseline.json"
BASELINE_PATH = ROOT / "pyright_plugin_baseline.json"
# pyright 子进程超时（秒）：与 run_helpers.run 的默认上限同量级，防僵死阻塞
# pre-commit/本地校验（FR-RISK-20261010-2902）。
PYRIGHT_TIMEOUT_SECONDS = 600

# 2026-10-10 技术核查（.scratch/round-20261010/pyright-evidence/）：pyright 的诊断
# message 跟随机器 UI/Node locale（zh-CN 环境输出中文文本），而基线以英文文本作为
# 比对键（file+rule+message），父环境语言不同会使 125 条存量全部误判为失配。
# 因此在 pyright 子进程环境中强制英文 locale；基线与比对键均不改（维护者 2026-10-10
# 指定的最小方案）。pyright CLI 本身无 locale 选项，只能经 VS Code 的 NLS 配置注入。
PYRIGHT_CHILD_ENV_OVERRIDE = {"VSCODE_NLS_CONFIG": '{"locale":"en-us"}'}

# 基线适用范围：整个宿主侧插件家族（`fukua_rpa/plugin_*.py`）。
# 2026-09-20 维护者裁定 D-9 采纳 (a)：把范围从最初的三个模块扩到全家族，
# 存量基线由 63 条扩到 145 条（见 `06_改进路线.md` §10）。新增插件模块仍不进基线、
# 必须严格通过（由 `known_modules` 机制强制）。
SCOPE_FILES = (
    "fukua_rpa/plugin_actions.py",
    "fukua_rpa/plugin_host_client.py",
    "fukua_rpa/plugin_host_process.py",
    "fukua_rpa/plugin_manifest.py",
    "fukua_rpa/plugin_package.py",
    "fukua_rpa/plugin_protocol.py",
    "fukua_rpa/plugin_registry.py",
)

BASELINE_FORMAT = "fukuaRPA.pyright_plugin_baseline"
BASELINE_VERSION = 1
# 初始锁定条数（145 = 3 模块 63 条 + 范围外 4 模块 82 条，D-9 采纳 (a) 后）。
# 基线只允许递减；任何增长都必须由维护者显式批准并更新此常量。
INITIAL_TOTAL = 145

_WHITESPACE = re.compile(r"\s+")

# SAFETY: `--write` 允许的最大一次性缩减比例。基线条目只能在被证明"确实不再复现"
# 时移除；一次性丢失超过该比例的条目几乎总是路径归一化或工具链故障，而不是真实
# 清零，故要求维护者显式 `--force`（FR-20260920-001 的 ② 号边界）。
_MAX_SHRINK_RATIO = 0.5


def pyright_available() -> bool:
    """Return True when the pyright CLI is importable in this interpreter."""

    import importlib.util

    return importlib.util.find_spec("pyright") is not None


def _relative_posix(path: str) -> str:
    """Normalize a pyright-reported path to a repo-relative POSIX path.

    DESIGN: 基线只允许保存**仓库相对、分隔符规范化**的路径，不得含机器前缀
    （盘符、用户名、临时目录等，见 `DEC-20260920-001`）。因此这里**只**按仓库根
    做相对化，不再依赖硬编码的 ``/codex/`` 标记：该标记在本机其他位置的
    ``/codex/`` 段先出现时会把路径错误截断（实测会使全部诊断被 scope 过滤掉），
    也可能把仓库外的路径伪装成仓库内路径。越出仓库的路径一律拒绝，避免绝对路径
    进入基线。

    Raises:
        ValueError: If *path* resolves outside the repository root.
    """

    value = str(path).replace("\\", "/")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        return candidate.resolve().relative_to(ROOT).as_posix()
    except ValueError as error:
        raise ValueError(f"路径越出仓库，拒绝归一化：{path!r}") from error


def _normalize_message(message: str) -> str:
    return _WHITESPACE.sub(" ", str(message)).strip()


def collect_diagnostics(
    config: str = PLUGIN_PYRIGHT_CONFIG,
    paths: tuple[str, ...] | None = None,
    scope_files: tuple[str, ...] | None = None,
) -> list[dict]:
    """Run pyright with *config* and return the error diagnostics in scope.

    Args:
        config: pyright project file to use (its strict settings apply).
        paths: Optional explicit file list; positional paths override the
            project's ``include`` (used to check modules outside the baseline).
        scope_files: Which reported files to keep; defaults to ``SCOPE_FILES``.

    Returns entries sorted by (file, line, column, rule, message) with 1-based
    ``line``/``column`` for human readability.
    """

    command = [
        sys.executable,
        "-m",
        "pyright",
        "--project",
        config,
        "--outputjson",
    ]
    if paths:
        command.extend(paths)
    child_env = dict(os.environ)
    child_env.update(PYRIGHT_CHILD_ENV_OVERRIDE)
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=PYRIGHT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        # FR-RISK-20261010-2902：发布工具链的子进程必须有界；语言服务器僵死时
        # 明确失败，不得让 pre-commit/本地校验无限阻塞。
        raise RuntimeError(
            f"pyright 在 {PYRIGHT_TIMEOUT_SECONDS}s 内未完成，已终止；"
            "请检查语言服务器状态后重试"
        ) from error
    payload = result.stdout.strip()
    if not payload:
        raise RuntimeError(
            "pyright 未产生 JSON 输出（退出码 "
            f"{result.returncode}）：{result.stderr.strip()[:400]}"
        )
    report = json.loads(payload)
    scope = set(scope_files if scope_files is not None else SCOPE_FILES)
    entries = []
    for item in report.get("generalDiagnostics", []):
        if item.get("severity") != "error":
            continue
        try:
            relative = _relative_posix(item.get("file", ""))
        except ValueError:
            # SAFETY: 越出仓库的诊断（第三方 stub 等）不可能是任何仓库相对的
            # scope 文件；直接排除，既不改写成绝对路径，也不中断整轮收集。
            continue
        if relative not in scope:
            continue
        start = item.get("range", {}).get("start", {})
        entries.append(
            {
                "file": relative,
                "line": int(start.get("line", 0)) + 1,
                "column": int(start.get("character", 0)) + 1,
                "rule": item.get("rule") or "unknown",
                "message": _normalize_message(item.get("message", "")),
            }
        )
    entries.sort(
        key=lambda e: (e["file"], e["line"], e["column"], e["rule"], e["message"])
    )
    return entries


def diagnostic_key(entry: dict) -> tuple[str, str, str]:
    """Comparison key: file + rule + message (line/column are informational)."""

    return (entry["file"], entry["rule"], entry["message"])


def all_plugin_modules() -> tuple[str, ...]:
    """Every first-party host-side plugin module (repo-relative POSIX paths)."""

    return tuple(
        sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "fukua_rpa").glob("plugin_*.py")
        )
    )


def collect_plugin_family_diagnostics() -> list[dict]:
    """Diagnostics across the whole host-side plugin family.

    The ratified baseline exemption covers only :data:`SCOPE_FILES`; this helper
    measures the *whole* family so the guard can (a) require new modules to be
    clean and (b) detect new debt in existing out-of-scope modules without
    exempting them.
    """

    modules = all_plugin_modules()
    return collect_diagnostics(paths=modules, scope_files=modules)


def load_baseline(path: Path = BASELINE_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_out_of_scope(current: list[dict], baseline: dict) -> dict:
    """Report per-module diagnostic counts **outside** the ratified scope.

    ``out_of_scope_counts`` was recorded on every save but never read back, which
    left rule 2 (「新增插件模块必须严格通过」) invisible in the report output: a
    maintainer reading a "OK" verification had no way to see that a newly added
    plugin module had started producing diagnostics, because nothing printed it.

    DESIGN: this is the authoritative rejection for **out-of-scope** debt (new
    modules and growth against the recorded ``out_of_scope_counts``), and it
    must be driven by the whole-family collection
    (:func:`collect_plugin_family_diagnostics`). :func:`compare` covers only the
    in-scope diagnostics: the ratified exemption is limited to
    :data:`SCOPE_FILES`, and the baseline note states that
    ``out_of_scope_counts`` "仅记录…用于禁止新增债务，不构成豁免" — so recorded
    out-of-scope debt must not be reported as ``added`` (that would make the CLI
    permanently red and contradict this helper's "no growth" verdict), while new
    out-of-scope debt must still fail here.

    FR-20261010-2601：此前 main() 传入 scope 过滤的 current，本函数恒空，承诺
    失真；FR-20261011-011：main() 改为把全家族结果只交给本函数，compare 与写盘
    仍用 scope 过滤后的条目。

    In-scope modules are skipped because their entries are already covered by the
    per-diagnostic ``added``/``stale`` comparison.
    """
    scope = set(SCOPE_FILES)
    current_counts: dict[str, int] = {}
    for entry in current:
        module = str(entry.get("file", ""))
        if module in scope:
            continue
        current_counts[module] = current_counts.get(module, 0) + 1
    recorded = {
        str(module): int(count)
        for module, count in (baseline.get("out_of_scope_counts") or {}).items()
    }
    new_modules = {
        module: count
        for module, count in current_counts.items()
        if module not in recorded
    }
    grown = {
        module: count - recorded[module]
        for module, count in current_counts.items()
        if module in recorded and count > recorded[module]
    }
    return {"new_modules": new_modules, "grown": grown}


def compare(current: list[dict], baseline: dict) -> dict:
    """Return the ratchet comparison between *current* and *baseline*."""

    current_counter = Counter(diagnostic_key(entry) for entry in current)
    baseline_counter = Counter(
        diagnostic_key(entry) for entry in baseline.get("diagnostics", [])
    )
    added = {
        key: count - baseline_counter.get(key, 0)
        for key, count in current_counter.items()
        if count > baseline_counter.get(key, 0)
    }
    stale = {
        key: count - current_counter.get(key, 0)
        for key, count in baseline_counter.items()
        if count > current_counter.get(key, 0)
    }
    return {
        "added": added,
        "stale": stale,
        "current_total": sum(current_counter.values()),
        "baseline_total": sum(baseline_counter.values()),
    }


def write_baseline(current: list[dict], path: Path = BASELINE_PATH, force: bool = False) -> dict:
    """Persist *current* as the new baseline.

    INVARIANT: 写盘与只读校验**共用同一套判定**——写盘前必须先
    ``compare(current, load_baseline())``，拒绝任何"新增/扩大"以及基线缺失以外的
    异常缩减；否则操作者可用 ``--write`` 把新增报错洗入基线，使守护测试必然通过
    （FR-20260920-001）。

    允许写入的情形：① 基线文件尚不存在（首次初始化）；② 与基线一致；③ 仅按条目
    减少且缩减幅度不超过 :data:`_MAX_SHRINK_RATIO`；④ 显式 ``--force``。

    Raises:
        SystemExit: 总量超限、存在新增条目或出现异常缩减且未给出 ``--force``。
    """

    total = len(current)
    if total > INITIAL_TOTAL and not force:
        raise SystemExit(
            f"拒绝写入：当前诊断 {total} 条超过初始锁定上限 {INITIAL_TOTAL} 条。"
            "基线只能递减；确需增长请由维护者批准后显式使用 --force。"
        )

    baseline_path = Path(path)
    if baseline_path.exists():
        baseline = load_baseline(baseline_path)
        report = compare(current, baseline)
        if not force:
            if report["added"]:
                raise SystemExit(
                    f"拒绝写入：当前存在新增/扩大报错 {len(report['added'])} 类，"
                    "基线只能递减、不得增加。请先修复这些报错；确属维护者批准的"
                    "例外请显式使用 --force。"
                )
            if report["current_total"] == 0 or (
                report["baseline_total"] > 0
                and report["current_total"]
                < report["baseline_total"] * (1 - _MAX_SHRINK_RATIO)
            ):
                raise SystemExit(
                    f"拒绝写入：当前 {report['current_total']} 条相对基线 "
                    f"{report['baseline_total']} 条异常缩减，疑似路径归一化或工具链"
                    "故障。确属真实清零或大幅减少时请显式使用 --force。"
                )

    known_modules = all_plugin_modules()
    family_counts: dict[str, int] = {}
    for entry in collect_plugin_family_diagnostics():
        family_counts[entry["file"]] = family_counts.get(entry["file"], 0) + 1
    payload = {
        "format": BASELINE_FORMAT,
        "version": BASELINE_VERSION,
        "pyright_config": PLUGIN_PYRIGHT_CONFIG,
        "initial_total": INITIAL_TOTAL,
        "scope_files": list(SCOPE_FILES),
        "comparison_key": ["file", "rule", "message"],
        "note": (
            "FR-015 基线棘轮：仅豁免下表已确认条目；新增插件模块不得进入基线；"
            "现有模块不得新增或扩大报错；改动使条目不再复现时必须同步移除；"
            "清零后删除本文件与 pyrightconfig.plugin_baseline.json，并把 scope_files 中的模块"
            "纳入 pyrightconfig.strict.json。line/column 仅作定位信息，不参与比对。"
            "范围沿革：初始 3 模块 63 条 → 2026-09-20 D-9 采纳 (a) 后为整个 plugin_* 家族 145 条。"
            "out_of_scope_counts 仅记录 scope_files 之外既有模块的诊断数，用于禁止新增债务，"
            "不构成豁免。"
        ),
        "total": total,
        "known_modules": list(known_modules),
        "out_of_scope_counts": {
            module: count
            for module, count in sorted(family_counts.items())
            if module not in SCOPE_FILES
        },
        "diagnostics": current,
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FR-015 插件数据层 pyright 基线棘轮")
    parser.add_argument(
        "--write",
        action="store_true",
        help="用当前状态重写基线（拒绝增长与异常缩减，除非同时给出 --force）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许基线增长（仅限维护者批准的例外）",
    )
    args = parser.parse_args(argv)

    if not pyright_available():
        print("pyright 未安装，无法校验基线。请先 `pip install pyright`。", file=sys.stderr)
        return 2

    # FR-20261010-2601：范围外模块的“新增/扩大”必须可见，因此这里取**全家族**
    # 收集；但 FR-20261011-011：只有范围外的**逐模块可见性**由
    # compare_out_of_scope 承担，compare 与写盘仍必须使用 scope 过滤后的条目。
    # 若把全家族结果直接交给 compare/写盘：
    #   ① 基线中已记录的既有范围外债务会被 compare 当成 added，CLI 恒红且与
    #      compare_out_of_scope 的“无增长”结论自相矛盾；
    #   ② write_baseline 会把范围外条目写进 diagnostics，使其从“记录”变成
    #      “豁免”，违反基线 note「out_of_scope_counts…不构成豁免」。
    family = collect_plugin_family_diagnostics()
    scope = set(SCOPE_FILES)
    current = [entry for entry in family if str(entry.get("file", "")) in scope]
    if args.write:
        payload = write_baseline(current, force=args.force)
        print(f"已写入基线：{payload['total']} 条 → {BASELINE_PATH}")
        return 0

    baseline = load_baseline()
    report = compare(current, baseline)
    print(
        f"当前 {report['current_total']} 条 / 基线 {report['baseline_total']} 条"
        f"（上限 {INITIAL_TOTAL}）"
    )
    if report["added"]:
        print("新增/扩大（必须修复，不得加入基线）：")
        for key, count in sorted(report["added"].items()):
            print(f"  +{count}  {key[0]}  [{key[1]}]  {key[2][:120]}")
    if report["stale"]:
        print("基线中已不再复现（必须从基线移除）：")
        for key, count in sorted(report["stale"].items()):
            print(f"  -{count}  {key[0]}  [{key[1]}]  {key[2][:120]}")
    # Rule 2 visibility: modules outside the ratified scope must be zero.
    # FR-20261010-2601：compare_out_of_scope 必须由全家族收集驱动，否则范围外
    # 条目在 scope 过滤中被丢弃，本检查恒空——CLI 单独校验的「新增插件模块/
    # 诊断增加」承诺从未生效。FR-20261011-011：只有本检查使用全家族，compare
    # 与写盘使用 scope 过滤后的 current（见 main 顶部说明）。
    out_of_scope = compare_out_of_scope(family, baseline)
    for module, count in sorted(out_of_scope["new_modules"].items()):
        print(f"新增插件模块（不得进入基线）：  +{count}  {module}")
    for module, count in sorted(out_of_scope["grown"].items()):
        print(f"插件模块诊断增加：  +{count}  {module}")
    if report["current_total"] > INITIAL_TOTAL:
        print(f"基线总量超过初始上限 {INITIAL_TOTAL}，违反只减不增。")
    ok = (
        not report["added"]
        and not report["stale"]
        and not out_of_scope["new_modules"]
        and not out_of_scope["grown"]
        and report["current_total"] <= INITIAL_TOTAL
    )
    print("OK" if ok else "不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
