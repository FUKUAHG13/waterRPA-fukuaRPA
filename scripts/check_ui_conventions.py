"""检查本次新增或修改的 Qt UI 代码是否遵守项目视觉约束。"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "fukua_rpa" / "ui"


@dataclass(frozen=True)
class Rule:
    code: str
    allowance: str
    message: str
    suggestion: str


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str
    message: str
    suggestion: str


RULES = {
    "UI001": Rule(
        "UI001",
        "hardcoded-color",
        "主题文件外出现硬编码颜色",
        "把颜色定义为 theme.py 的语义 token，业务代码只引用 token 或动态属性",
    ),
    "UI002": Rule(
        "UI002",
        "local-style",
        "主题文件外直接调用 setStyleSheet",
        "把样式放入 theme.py，并通过 objectName、variant 或其他动态属性选择语义状态",
    ),
    "UI003": Rule(
        "UI003",
        "fixed-geometry",
        "新增固定或正数最小/最大尺寸",
        "优先使用布局、QSizePolicy、sizeHint 或 components.py 中的内容适配 helper",
    ),
    "UI004": Rule(
        "UI004",
        "layout-metrics",
        "新增硬编码的正数布局边距或间距",
        "复用现有组件或语义度量；零边距可以直接使用",
    ),
    "UI005": Rule(
        "UI005",
        "font-size",
        "新增局部硬编码字号",
        "沿用应用字体和主题倍率；确需强调时使用主题语义或带理由的例外",
    ),
}

THEME_PATH = "fukua_rpa/ui/theme.py"
FIXED_GEOMETRY_METHODS = frozenset(
    {
        "setFixedHeight",
        "setFixedSize",
        "setFixedWidth",
    }
)
BOUNDED_GEOMETRY_METHODS = frozenset(
    {
        "setMaximumHeight",
        "setMaximumSize",
        "setMaximumWidth",
        "setMinimumHeight",
        "setMinimumSize",
        "setMinimumWidth",
    }
)
LAYOUT_METRIC_METHODS = frozenset(
    {
        "setContentsMargins",
        "setHorizontalSpacing",
        "setSpacing",
        "setVerticalSpacing",
    }
)
FONT_SIZE_METHODS = frozenset({"setPixelSize", "setPointSize", "setPointSizeF"})
HEX_COLOR_RE = re.compile(
    r"(?<![0-9A-Za-z_])#[0-9A-Fa-f]{8}(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Za-z_])#[0-9A-Fa-f]{6}(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Za-z_])#[0-9A-Fa-f]{4}(?![0-9A-Fa-f])"
    r"|(?<![0-9A-Za-z_])#[0-9A-Fa-f]{3}(?![0-9A-Fa-f])"
)
RGB_COLOR_RE = re.compile(r"\brgba?\s*\(", re.IGNORECASE)
COLOR_CONTEXT_RE = re.compile(
    r"\b(?:background|border|color|fill|gradient|shadow|stroke)\b|<font\b",
    re.IGNORECASE,
)
DIRECTIVE_RE = re.compile(
    r"#\s*UI-CONVENTION:\s*allow\s+([A-Za-z0-9_-]+)\s+reason\s*=\s*(.*?)\s*$"
)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitDiffError(RuntimeError):
    """无法可靠取得 Git 差异时阻止严格检查静默放行。"""


def normalize_path(path: str | Path) -> str:
    """把仓库路径规范为 Git 使用的正斜杠相对路径。"""

    value = str(path).replace("\\", "/").strip()
    if value.startswith("./"):
        value = value[2:]
    return value


def _method_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _numeric_constant(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        value = float(node.operand.value)
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _has_nonzero_numeric_argument(call: ast.Call) -> bool:
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        value is not None and value != 0
        for argument in arguments
        for child in ast.walk(argument)
        if (value := _numeric_constant(child)) is not None
    )


def _all_numeric_color_arguments(call: ast.Call) -> bool:
    values = [_numeric_constant(argument) for argument in call.args]
    return len(values) >= 3 and all(value is not None for value in values)


def _is_qcolor_constructor(call: ast.Call) -> bool:
    method = _method_name(call)
    if method == "QColor":
        return True
    if method not in {"fromRgb", "fromRgba"} or not isinstance(
        call.func, ast.Attribute
    ):
        return False
    owner = call.func.value
    if isinstance(owner, ast.Name):
        return owner.id == "QColor"
    return isinstance(owner, ast.Attribute) and owner.attr == "QColor"


def _looks_like_color_literal(value: str) -> bool:
    if RGB_COLOR_RE.search(value):
        return True
    if HEX_COLOR_RE.fullmatch(value.strip()):
        return True
    return bool(HEX_COLOR_RE.search(value) and COLOR_CONTEXT_RE.search(value))


def _docstring_node_ids(tree: ast.AST) -> frozenset[int]:
    nodes: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for owner in ast.walk(tree):
        if not isinstance(owner, owners) or not owner.body:
            continue
        first = owner.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(id(first.value))
    return frozenset(nodes)


def _node_selected(node: ast.AST, selected_lines: frozenset[int] | None) -> bool:
    if selected_lines is None:
        return True
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    return any(start <= line <= end for line in selected_lines)


def _parse_directive(line: str) -> tuple[str, str] | None:
    match = DIRECTIVE_RE.search(line)
    if match is None:
        return None
    target = match.group(1)
    reason = match.group(2).strip()
    if target not in {rule.code for rule in RULES.values()} | {
        rule.allowance for rule in RULES.values()
    }:
        return None
    if len(reason) < 4:
        return None
    return target, reason


def _collect_directives(source: str) -> dict[int, tuple[str, str] | None]:
    """只从真实 Python 注释中收集例外，字符串内容不能充当豁免。"""

    directives: dict[int, tuple[str, str] | None] = {}
    try:
        tokens = tokenize.generate_tokens(StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT or "UI-CONVENTION:" not in token.string:
                continue
            directives[token.start[0]] = _parse_directive(token.string)
    except (IndentationError, tokenize.TokenError):
        # ast.parse 会在随后给出 UI099；这里保留已成功读取的注释即可。
        pass
    return directives


def _has_allowance(
    directives: Mapping[int, tuple[str, str] | None],
    node: ast.AST,
    rule: Rule,
) -> bool:
    start = max(1, getattr(node, "lineno", 1))
    end = max(start, getattr(node, "end_lineno", start) or start)
    first = max(1, start - 2)
    last = end
    for line_number in range(first, last + 1):
        directive = directives.get(line_number)
        if directive is None:
            continue
        target, _reason = directive
        if target in {rule.code, rule.allowance}:
            return True
    return False


def _finding(path: str, node: ast.AST, rule: Rule) -> Finding:
    return Finding(
        path=path,
        line=max(1, getattr(node, "lineno", 1)),
        code=rule.code,
        message=rule.message,
        suggestion=rule.suggestion,
    )


def check_source(
    source: str,
    *,
    path: str,
    selected_lines: Iterable[int] | None = None,
) -> tuple[Finding, ...]:
    """检查一份 UI Python 源码，可把范围限制在 Git 新增行。"""

    normalized_path = normalize_path(path)
    selected = None if selected_lines is None else frozenset(selected_lines)
    findings: set[Finding] = set()
    directives = _collect_directives(source)

    for line_number, directive in directives.items():
        if selected is not None and line_number not in selected:
            continue
        if directive is None:
            findings.add(
                Finding(
                    normalized_path,
                    line_number,
                    "UI000",
                    "UI-CONVENTION 例外格式、规则名或理由无效",
                    "使用 `# UI-CONVENTION: allow <规则名> reason=<具体原因>`，理由至少四个字符",
                )
            )

    try:
        tree = ast.parse(source, filename=normalized_path)
    except SyntaxError as exc:
        line = max(1, int(exc.lineno or 1))
        if selected is None or selected:
            findings.add(
                Finding(
                    normalized_path,
                    line,
                    "UI099",
                    "UI Python 文件无法解析",
                    str(exc.msg),
                )
            )
        return tuple(sorted(findings))

    is_theme = normalized_path == THEME_PATH
    color_rule = RULES["UI001"]
    style_rule = RULES["UI002"]
    geometry_rule = RULES["UI003"]
    metric_rule = RULES["UI004"]
    font_rule = RULES["UI005"]

    if not is_theme:
        docstring_nodes = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstring_nodes:
                continue
            if not _looks_like_color_literal(node.value) or not _node_selected(
                node, selected
            ):
                continue
            if not _has_allowance(directives, node, color_rule):
                findings.add(_finding(normalized_path, node, color_rule))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _node_selected(node, selected):
            continue
        method = _method_name(node)
        if method is None:
            continue

        if (
            not is_theme
            and method == "setStyleSheet"
            and not _has_allowance(directives, node, style_rule)
        ):
            findings.add(_finding(normalized_path, node, style_rule))

        if (
            not is_theme
            and _is_qcolor_constructor(node)
            and _all_numeric_color_arguments(node)
            and not _has_allowance(directives, node, color_rule)
        ):
            findings.add(_finding(normalized_path, node, color_rule))

        if method in FIXED_GEOMETRY_METHODS and not _has_allowance(
            directives, node, geometry_rule
        ):
            findings.add(_finding(normalized_path, node, geometry_rule))

        if (
            method in BOUNDED_GEOMETRY_METHODS
            and _has_nonzero_numeric_argument(node)
            and not _has_allowance(
                directives, node, geometry_rule
            )
        ):
            findings.add(_finding(normalized_path, node, geometry_rule))

        if (
            method in LAYOUT_METRIC_METHODS
            and _has_nonzero_numeric_argument(node)
            and not _has_allowance(directives, node, metric_rule)
        ):
            findings.add(_finding(normalized_path, node, metric_rule))

        if method in FONT_SIZE_METHODS and not _has_allowance(
            directives, node, font_rule
        ):
            findings.add(_finding(normalized_path, node, font_rule))

    return tuple(sorted(findings))


def parse_unified_diff(diff_text: str) -> dict[str, frozenset[int]]:
    """从统一 diff 中提取每个新文件的新增/修改后行号。

    FR-20260811-D15-002: 文件头（``+++ ``）仅在 hunk 之外识别（new_line is None）；
    hunk 体内按首字符三分类（+ 新增 / - 删除 / 其它上下文）。修复前正文行
    ``+++ marker``（新增行内容以 ``+++ `` 开头）会被误判为文件头并把 current_path
    改写为伪路径，导致后续 hunk 被过滤丢弃、门禁 fail-open（报告 0 文件通过）。
    """
    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int | None = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            # 新文件段开始：重置 hunk 状态。collect_changed_ui_lines 的单次 diff
            # 常含多个文件，若不重置，第二文件段的 ``+++ `` 头会因 new_line 非 None
            # 被误判为新增行，导致后续文件路径错乱、门禁漏检（P1 回归）。
            new_line = None
            continue
        if line.startswith("+++ ") and new_line is None:
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                current_path = None
            else:
                current_path = normalize_path(
                    raw_path[2:] if raw_path.startswith("b/") else raw_path
                )
                changed.setdefault(current_path, set())
            new_line = None
            continue

        match = HUNK_RE.match(line)
        if match is not None:
            new_line = int(match.group(1))
            continue

        if current_path is None or new_line is None:
            continue
        if line.startswith("+"):
            # hunk 内新增行：以 + 开头即计入（含内容形如 ++xxx / +++xxx 的行）。
            changed[current_path].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            # 删除行不占用新文件行号。
            continue
        elif line.startswith("\\ No newline at end of file"):
            continue
        else:
            new_line += 1

    return {path: frozenset(lines) for path, lines in changed.items() if lines}


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,  # FR-20260812-D15-001: 防止 git 挂起无限阻塞
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GitDiffError(f"Git 命令失败：git {' '.join(args)}") from exc
    return result.stdout


def _default_base_ref() -> str:
    candidates = ("@{upstream}", "origin/main", "origin/master", "HEAD^")
    for candidate in candidates:
        try:
            resolved = _run_git("rev-parse", "--verify", "--quiet", candidate).strip()
        except GitDiffError:
            continue
        if resolved:
            return resolved
    # FR-20260812 建议-打包-6: 仅当以上候选全部不可用（空仓库/无提交）才回退
    # HEAD；此时 checked_count=0 会让门禁恒通过，必须显式警告而不是静默。
    print(
        "[check_ui_conventions] 警告：未找到可用基线（upstream/main/HEAD^），"
        "回退到 HEAD，UI 差异检查将不覆盖任何既有提交，可能漏检。"
    )
    return "HEAD"


def collect_changed_ui_lines(base_ref: str | None = None) -> dict[str, frozenset[int]]:
    """收集相对基线的 UI 新增行，并把未跟踪 UI 文件视为全新文件。"""

    baseline = base_ref or _default_base_ref()
    diff = _run_git(
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--unified=0",
        baseline,
        "--",
        "fukua_rpa/ui",
    )
    changed = {
        path: set(lines)
        for path, lines in parse_unified_diff(diff).items()
        if path.startswith("fukua_rpa/ui/") and path.endswith(".py")
    }

    untracked = _run_git(
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "fukua_rpa/ui",
    )
    for raw_path in untracked.splitlines():
        path = normalize_path(raw_path)
        absolute = ROOT / path
        if not path.endswith(".py") or not absolute.is_file():
            continue
        line_count = len(absolute.read_text(encoding="utf-8").splitlines())
        changed[path] = set(range(1, line_count + 1))

    return {path: frozenset(lines) for path, lines in changed.items() if lines}


def collect_all_ui_sources() -> tuple[str, ...]:
    return tuple(
        normalize_path(path.relative_to(ROOT))
        for path in sorted(UI_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def check_paths(
    paths: Iterable[str | Path],
    *,
    selected_lines: Mapping[str, Iterable[int]] | None = None,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for raw_path in paths:
        path = normalize_path(raw_path)
        if not path.startswith("fukua_rpa/ui/") or not path.endswith(".py"):
            continue
        absolute = ROOT / path
        if not absolute.is_file():
            continue
        lines = None if selected_lines is None else selected_lines.get(path, ())
        findings.extend(
            check_source(
                absolute.read_text(encoding="utf-8"),
                path=path,
                selected_lines=lines,
            )
        )
    return tuple(sorted(set(findings)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查 fukua_rpa/ui 中新增或修改的代码是否遵守 UI 开发规范。"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--changed",
        action="store_true",
        help="检查相对 Git 基线的新增/修改行（默认）",
    )
    mode.add_argument(
        "--all",
        action="store_true",
        help="盘点全部 UI Python 文件；历史发现不会自动获得例外",
    )
    parser.add_argument(
        "--base-ref",
        help="--changed 使用的 Git 基线提交或引用；CI 应显式传入",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="发现违规时返回非零退出码",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="显式检查的仓库相对 UI Python 文件；提供后按完整文件检查",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.base_ref and args.all:
        print("错误：--base-ref 只能与 --changed 一起使用。", file=sys.stderr)
        return 2

    try:
        if args.paths:
            findings = check_paths(args.paths)
            checked_count = len(args.paths)
        elif args.all:
            paths = collect_all_ui_sources()
            findings = check_paths(paths)
            checked_count = len(paths)
        else:
            changed_lines = collect_changed_ui_lines(args.base_ref)
            findings = check_paths(changed_lines, selected_lines=changed_lines)
            # FR-20260811-D15-4: 报告实际检查的行数（与 check_paths 的过滤一致），
            # 而非请求行数，避免 diff 解析异常时打印"已检查 0 个文件"的误导输出。
            checked_count = sum(
                len(lines)
                for path, lines in changed_lines.items()
                if path.startswith("fukua_rpa/ui/")
                and path.endswith(".py")
                and (ROOT / path).is_file()
            )
    except GitDiffError as exc:
        print(f"UI 规范检查无法取得可靠差异：{exc}", file=sys.stderr)
        return 2 if args.strict else 0

    if not findings:
        print(f"UI 规范检查通过：已检查 {checked_count} 行 UI 新增/修改代码。")
        return 0

    print(f"UI 规范检查发现 {len(findings)} 项问题（{checked_count} 行 UI 新增/修改代码）：")
    for item in findings:
        print(
            f"{item.path}:{item.line}: {item.code} {item.message}；建议：{item.suggestion}"
        )
    print("例外格式见 docs/UI开发规范.md；专项盘点请使用 --all。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
