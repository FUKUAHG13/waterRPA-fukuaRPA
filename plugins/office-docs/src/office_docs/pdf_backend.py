"""PDF 后端抽象与两种实现。

* ``PypdfBackend``：基于 ``pypdf``（惰性导入）做文本提取与合并/拆分。
* ``MockPdfBackend``：内置几页固定文本，用于单元测试与无依赖环境的逻辑冒烟。

提取文本的能力对 RPA 很实用：例如「读取 PDF 里的发票金额」「在扫描件里搜索
关键词并定位页码」。这些都属于本地离线处理，不联网。
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Any


def parse_pages(pages: str | None) -> list[int] | None:
    """把 "1-3,5" 解析成 [1,2,3,5]（1-based）；None 表示全部。"""

    if not pages:
        return None
    out: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"无法解析页码范围: {part!r}（页码必须是整数）") from None
            if lo < 1 or hi < 1:
                raise ValueError(f"页码必须 >= 1，收到: {part!r}")
            # FR-a01-004: 逆序范围（如 "5-1"）显式拒绝，与 pdf_split 的
            # "起始页不能大于结束页" 语义一致，避免静默产出空文本。
            if lo > hi:
                raise ValueError(
                    f"页码范围起始页不能大于结束页: {part!r}（{lo} > {hi}）"
                )
            out.extend(range(lo, hi + 1))
        elif part:
            try:
                num = int(part)
            except ValueError:
                raise ValueError(f"无法解析页码: {part!r}（页码必须是整数）") from None
            if num < 1:
                raise ValueError(f"页码必须 >= 1，收到: {num}")
            out.append(num)
    return out


# FR-20260809-1202 (ReDoS): re 的 C 匹配器持有 GIL，线程级超时无法生效
# （worker 卡死会连带阻塞主线程），因此采用"启发式拒绝 + 输入规模限制"的多层
# 防御。启发式非完备：仅覆盖最常见的"嵌套量词"病态结构，命中的病态模式降级为
# "无结果"（返回 []）；而非病态的非法正则（如 "["）会在 re.compile 抛 re.error，
# 由 execute_action 转成失败响应——二者行为不同，前者静默空结果、后者显式失败。
# （DOC-a01-001：原注释把二者统称为"降级为无结果"，与实际不符。）
_MAX_QUERY_LENGTH = 512
_PAGE_TEXT_LIMIT = 300_000
_MAX_REGEX_MATCHES = 10_000
_CATASTROPHIC_PROBES = (
    # 嵌套量词：(a+)+ / (a*)* / (x+)* —— 组内量词再跟外层量词，指数回溯。
    # (?<!\\) 排除转义字面括号（如 \(a+\)+ 是字面 "(a+)" 重复，非嵌套分组）。
    re.compile(r"(?<!\\)\([^()]*[+*?][^()]*\)[+*?]"),
    # 嵌套量词 + 范围：(a+){m,n} 同样指数回溯。
    re.compile(r"(?<!\\)\([^()]*[+*?][^()]*\)\{"),
    # 双层括号内嵌量词：\((a+)+\) / (a+(b+))+ —— 内层分组必须含量词才算病态
    # （(x(y)z)+ 等内层无量词的嵌套分组是安全的，不得误拒）。
    re.compile(r"(?<!\\)\([^()]*(?<!\\)\([^()]*[+*?][^()]*\)[^()]*\)[+*?]"),
)


def _reject_catastrophic_pattern(query: str) -> bool:
    """Heuristic ReDoS guard; True means the query should be rejected."""
    if len(query) > _MAX_QUERY_LENGTH:
        return True
    return any(probe.search(query) for probe in _CATASTROPHIC_PROBES)


class PdfBackend(ABC):
    @abstractmethod
    def page_count(self, path: str) -> int: ...

    @abstractmethod
    def extract_text(self, path: str, pages: str | None = None) -> str: ...

    @abstractmethod
    def search(self, path: str, query: str, mode: str) -> list[dict[str, Any]]:
        """返回命中列表，每项 {page, snippet, index}。mode ∈ {contains,regex}。"""

    @abstractmethod
    def merge(self, inputs: list[str], output: str) -> int:
        """合并多个 PDF，返回输出文件页数。

        出于数据安全考虑，若 ``output`` 已存在则拒绝覆盖（抛 ``FileExistsError``）。
        """

    @abstractmethod
    def split(self, path: str, ranges: str, output_dir: str) -> list[str]:
        """按 ranges("1-2,3") 拆分，返回生成的文件路径列表。

        出于数据安全考虑，若某个拆分输出文件已存在则拒绝覆盖（抛 ``FileExistsError``）。
        """

    def close(self) -> None:  # noqa: B027 - 接口兼容桩：PDF 后端无需特殊清理，子类可选择性实现
        """释放资源（PDF 后端无需特殊清理，提供接口兼容）。"""


class PypdfBackend(PdfBackend):
    def _reader(self, path: str):
        # FR-20260812-D9-001: pypdf>=6 的 PdfReader 支持上下文管理器；调用点
        # 以 with 包裹确保 reader 缓存及时释放并与 merger/writer 风格一致。
        # （pypdf 4.x 无 __enter__/__exit__，已同步提高 requirements 下限。）
        from pypdf import PdfReader  # 惰性

        return PdfReader(path)

    def page_count(self, path: str) -> int:
        with self._reader(path) as reader:
            return len(reader.pages)

    def extract_text(self, path: str, pages: str | None = None) -> str:
        with self._reader(path) as reader:
            wanted = parse_pages(pages)
            chunks: list[str] = []
            for i, page in enumerate(reader.pages, start=1):
                if wanted is not None and i not in wanted:
                    continue
                chunks.append(page.extract_text() or "")
            return "\n".join(chunks)

    def search(self, path: str, query: str, mode: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        pattern = None
        if mode == "regex":
            # FR-20260809-1202: 拒绝病态正则（嵌套量词等指数回溯结构）与超长
            # 查询，降级为"无结果"而非挂起插件宿主。
            if _reject_catastrophic_pattern(str(query)):
                return []
            pattern = re.compile(query)
        with self._reader(path) as reader:
            for i, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if len(text) > _PAGE_TEXT_LIMIT:
                    text = text[:_PAGE_TEXT_LIMIT]
                if mode == "regex" and pattern:
                    for m in pattern.finditer(text):
                        matches.append({"page": i, "index": m.start(), "snippet": m.group(0)})
                        if len(matches) >= _MAX_REGEX_MATCHES:
                            return matches
                else:
                    idx = text.find(query)
                    while idx != -1:
                        snippet = text[max(0, idx - 20): idx + len(query) + 20]
                        matches.append({"page": i, "index": idx, "snippet": snippet})
                        idx = text.find(query, idx + 1)
        return matches

    def merge(self, inputs: list[str], output: str) -> int:
        # 数据安全（修复潜在 P2）：输出已存在则拒绝覆盖，避免静默破坏既有文件。
        if os.path.exists(output):
            raise FileExistsError(
                f"PDF 合并输出已存在，拒绝覆盖：{output}（请换用其它路径，或先删除旧文件）"
            )
        # FR-20260813-D10-002（专项修复）：pypdf>=6.0 已移除 PdfMerger，合并改用
        # PdfWriter.append(path)；此前代码 import PdfMerger 在 6.x 直接 ImportError，
        # 真实合并路径从未被任何测试覆盖（mock 整体替换 pypdf 掩盖了该缺陷）。
        from pypdf import PdfWriter  # 惰性

        writer = PdfWriter()
        try:
            for p in inputs:
                writer.append(p)
            writer.write(output)
        finally:
            writer.close()
        return self.page_count(output)

    def split(self, path: str, ranges: str, output_dir: str) -> list[str]:
        from pypdf import PdfWriter  # 惰性

        os.makedirs(output_dir, exist_ok=True)

        # 第一阶段：解析所有段并校验输出路径均不冲突，避免部分写入后遗留孤儿文件。
        dests: list[tuple[int, int, str]] = []
        with self._reader(path) as reader:
            for seg in (ranges or "").split(","):
                seg = seg.strip()
                if not seg:
                    continue
                if "-" in seg:
                    a, b = seg.split("-", 1)
                    try:
                        lo, hi = int(a), int(b)
                    except ValueError:
                        raise ValueError(
                            f"无法解析拆分范围: {seg!r}（页码必须是整数）"
                        ) from None
                else:
                    try:
                        lo = hi = int(seg)
                    except ValueError:
                        raise ValueError(
                            f"无法解析拆分范围: {seg!r}（页码必须是整数）"
                        ) from None
                if lo < 1 or hi < 1:
                    raise ValueError(
                        f"pdf_split 页码必须 >= 1，收到: {seg!r}"
                    )
                if lo > hi:
                    raise ValueError(
                        f"拆分范围起始页不能大于结束页: {seg!r}（{lo} > {hi}）"
                    )
                # FR-20260809-1204: 越界页码必须显式报错，不得静默跳过生成空/部分
                # PDF（此前 99-100 之类范围会产出看似成功但无内容的空文件）。
                if hi > len(reader.pages):
                    raise ValueError(
                        f"pdf_split 页码越界: {seg!r}（文档共 {len(reader.pages)} 页，"
                        f"请求到第 {hi} 页）"
                    )
                dest = os.path.join(output_dir, f"split_{lo}-{hi}.pdf")
                if os.path.exists(dest):
                    raise FileExistsError(
                        f"拒绝覆盖已存在的输出文件: {dest}"
                    )
                dests.append((lo, hi, dest))

            # 第二阶段：所有路径校验通过后执行写入（reader 生命周期内完成）。
            out_paths: list[str] = []
            for lo, hi, dest in dests:
                writer = PdfWriter()
                for i in range(lo - 1, hi):
                    if 0 <= i < len(reader.pages):
                        writer.add_page(reader.pages[i])
                writer.write(dest)
                writer.close()
                out_paths.append(os.path.abspath(dest))
        return out_paths


class MockPdfBackend(PdfBackend):
    _PAGES = [
        "第一章 介绍 fukuaRPA 自动化",
        "第二章 安装与配置 环境变量",
        "第三章 插件系统 与 离线 OCR",
        "第四章 计划任务 与 浏览器自动化",
    ]

    def page_count(self, path: str) -> int:
        return len(self._PAGES)

    def extract_text(self, path: str, pages: str | None = None) -> str:
        wanted = parse_pages(pages)
        if wanted is None:
            return "\n".join(self._PAGES)
        return "\n".join(self._PAGES[i - 1] for i in wanted if 1 <= i <= len(self._PAGES))

    def search(self, path: str, query: str, mode: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        if mode == "regex" and _reject_catastrophic_pattern(str(query)):
            # FR-20260809-1202: 与真实后端一致的病态正则拒绝。
            return []
        pattern = re.compile(query) if mode == "regex" else None
        for i, text in enumerate(self._PAGES, start=1):
            if pattern:
                for m in pattern.finditer(text):
                    matches.append({"page": i, "index": m.start(), "snippet": m.group(0)})
                    if len(matches) >= _MAX_REGEX_MATCHES:
                        return matches
            else:
                idx = text.find(query)
                while idx != -1:
                    matches.append({"page": i, "index": idx, "snippet": text[max(0, idx - 10): idx + len(query) + 10]})
                    idx = text.find(query, idx + 1)
        return matches

    def merge(self, inputs: list[str], output: str) -> int:
        # 与真实后端保持一致：输出已存在则拒绝覆盖。
        if os.path.exists(output):
            raise FileExistsError(f"PDF 合并输出已存在，拒绝覆盖：{output}")
        # Mock 写一个占位文件，证明路径回传链路可用。
        with open(output, "wb") as handle:
            handle.write(b"mock-merged-pdf")
        return max(1, len(inputs))

    def split(self, path: str, ranges: str, output_dir: str) -> list[str]:
        os.makedirs(output_dir, exist_ok=True)
        out_paths: list[str] = []
        for seg in (ranges or "").split(","):
            seg = seg.strip()
            if not seg:
                continue
            dest = os.path.join(output_dir, f"split_{seg}.pdf")
            # 与真实后端保持一致：拆分输出已存在则拒绝覆盖。
            if os.path.exists(dest):
                raise FileExistsError(f"PDF 拆分输出已存在，拒绝覆盖：{dest}")
            with open(dest, "wb") as handle:
                handle.write(f"mock-split-{seg}".encode())
            out_paths.append(os.path.abspath(dest))
        return out_paths


def create_pdf_backend(name: str = "pypdf") -> PdfBackend:
    if name == "mock":
        return MockPdfBackend()
    if name in ("pypdf", "pdf"):
        return PypdfBackend()
    raise ValueError(f"未知 PDF 后端：{name!r}（可选：pypdf / mock）")
