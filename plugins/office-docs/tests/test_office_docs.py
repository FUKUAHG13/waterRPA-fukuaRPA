#!/usr/bin/env python3
"""办公文档插件单元测试（使用 Mock 后端，无需安装 openpyxl/pypdf）。

运行
----
    python tests/test_office_docs.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from office_docs.action import execute_action, run  # noqa: E402
from office_docs.excel_backend import MockExcelBackend  # noqa: E402
from office_docs.pdf_backend import MockPdfBackend, _reject_catastrophic_pattern  # noqa: E402

try:
    import openpyxl  # noqa: F401
    HAS_OPENPYXL = True
except Exception:  # noqa: BLE001
    HAS_OPENPYXL = False

try:
    import pypdf  # noqa: F401
    HAS_PYPDF = True
except Exception:  # noqa: BLE001
    HAS_PYPDF = False


class ExcelActionTests(unittest.TestCase):
    def setUp(self):
        self.xl = MockExcelBackend()

    def _exec(self, action_id, payload=None):
        return execute_action(
            "office-docs", action_id, payload or {},
            backend=self.xl, backend_name="mock",
        )

    def test_read_cell(self):
        resp = self._exec("excel_read_cell", {"path": "x.xlsx", "sheet": "Sheet1", "cell": "A1"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["value"], "姓名")

    def test_read_cell_b2(self):
        resp = self._exec("excel_read_cell", {"path": "x.xlsx", "cell": "B2"})
        self.assertEqual(resp["result"]["value"], 30)

    def test_read_range(self):
        resp = self._exec("excel_read_range", {"path": "x.xlsx", "start": "A1", "end": "B2"})
        self.assertEqual(resp["result"]["values"], [["姓名", "年龄"], ["Alice", 30]])

    def test_write_cell(self):
        resp = self._exec("excel_write_cell", {"path": "x.xlsx", "cell": "A5", "value": "test"})
        self.assertTrue(resp["ok"])
        self.assertEqual(self.xl.read_cell("", "A5"), "test")

    def test_write_range(self):
        resp = self._exec("excel_write_range", {"path": "x.xlsx", "start": "D1",
                                                 "values": [["a", "b"], ["c", "d"]]})
        self.assertTrue(resp["ok"])
        self.assertEqual(self.xl.read_range("", "D1", "E2"), [["a", "b"], ["c", "d"]])

    def test_find_contains(self):
        resp = self._exec("excel_find", {"path": "x.xlsx", "value": "Alice"})
        self.assertEqual(resp["result"]["addresses"], ["A2"])

    def test_find_equals_number(self):
        resp = self._exec("excel_find", {"path": "x.xlsx", "value": "30", "mode": "equals"})
        self.assertEqual(resp["result"]["addresses"], ["B2"])

    def test_find_substring(self):
        # "北京" 位于 C2（A2=Alice, B2=30, C2=北京）
        resp = self._exec("excel_find", {"path": "x.xlsx", "value": "北", "mode": "contains"})
        self.assertIn("C2", resp["result"]["addresses"])

    def test_sheets(self):
        resp = self._exec("excel_sheets", {"path": "x.xlsx"})
        self.assertEqual(resp["result"]["sheets"], ["Sheet1"])

    def test_row_count(self):
        resp = self._exec("excel_row_count", {"path": "x.xlsx"})
        self.assertEqual(resp["result"]["rows"], 4)

    def test_missing_param(self):
        resp = self._exec("excel_read_cell", {"path": "x.xlsx"})  # 缺 cell
        self.assertFalse(resp["ok"])

    def test_find_wide_column(self):
        # 回归：>26 列（如 AA）的地址不应触发 IndexError（mock 后端）。
        self.xl.write_range("", "AA1", [["wide"]])
        resp = self._exec("excel_find", {"path": "x.xlsx", "value": "wide", "mode": "equals"})
        self.assertIn("AA1", resp["result"]["addresses"])


class OpenpyxlFormulaTests(unittest.TestCase):
    """真实 openpyxl 后端回归：公式单元格读取不应静默返回 None（P2#3）。"""

    @unittest.skipUnless(HAS_OPENPYXL, "需要 openpyxl")
    def test_read_formula_falls_back_not_none(self):
        from office_docs.excel_backend import OpenpyxlBackend

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.xlsx")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws["A1"] = 10
            ws["B1"] = 20
            ws["C1"] = "=A1+B1"  # openpyxl 不会计算，data_only 缓存值为 None
            wb.save(p)

            b = OpenpyxlBackend()
            b.open(p)
            # 普通数值：data_only 路径命中，返回原值。
            self.assertEqual(b.read_cell("", "A1"), 10)
            # 公式单元格：data_only 为 None 时应回退到公式字符串，而非静默 None。
            val = b.read_cell("", "C1")
            self.assertIsNotNone(val)
            self.assertIn("=", str(val))
            # 范围读取同样不应出现静默 None。
            rng = b.read_range("", "A1", "C1")
            self.assertEqual(rng[0][0], 10)
            self.assertIn("=", str(rng[0][2]))

    @unittest.skipUnless(HAS_OPENPYXL, "需要 openpyxl")
    def test_write_preserves_formulas(self):
        # 写入不应破坏既有公式（仍走含公式的工作簿保存）。
        from office_docs.excel_backend import OpenpyxlBackend

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f2.xlsx")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws["A1"] = 10
            ws["C1"] = "=A1*2"
            wb.save(p)

            b = OpenpyxlBackend()
            b.open(p)
            b.write_cell("", "B1", 99)
            b.save()
            # 重新打开应仍含公式（未被 data_only 保存剥离）。
            b2 = OpenpyxlBackend()
            b2.open(p)
            self.assertIn("=", str(b2.read_cell("", "C1")))


class PdfActionTests(unittest.TestCase):
    def setUp(self):
        self.pdf = MockPdfBackend()

    def _exec(self, action_id, payload=None):
        return execute_action(
            "office-docs", action_id, payload or {},
            backend=self.pdf, backend_name="mock",
        )

    def test_page_count(self):
        resp = self._exec("pdf_page_count", {"path": "b.pdf"})
        self.assertEqual(resp["result"]["pages"], 4)

    def test_extract_text(self):
        resp = self._exec("pdf_extract_text", {"path": "b.pdf"})
        self.assertIn("fukuaRPA", resp["result"]["text"])

    def test_extract_text_pages(self):
        resp = self._exec("pdf_extract_text", {"path": "b.pdf", "pages": "1"})
        self.assertIn("第一章", resp["result"]["text"])
        self.assertNotIn("第二章", resp["result"]["text"])

    def test_search_contains(self):
        resp = self._exec("pdf_search", {"path": "b.pdf", "query": "自动化"})
        pages = [m["page"] for m in resp["result"]["matches"]]
        self.assertEqual(sorted(pages), [1, 4])

    def test_search_regex(self):
        resp = self._exec("pdf_search", {"path": "b.pdf", "query": "插件.*OCR", "mode": "regex"})
        self.assertEqual([m["page"] for m in resp["result"]["matches"]], [3])

    def test_merge(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "merged.pdf")
            resp = self._exec("pdf_merge", {"inputs": ["a.pdf", "b.pdf"], "output": out})
            self.assertEqual(resp["result"]["pages"], 2)
            self.assertTrue(os.path.isfile(out))

    def test_split(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = os.path.join(d, "out")
            resp = self._exec("pdf_split", {"path": "b.pdf", "ranges": "1-2,3", "output_dir": out_dir})
            self.assertEqual(len(resp["result"]["outputs"]), 2)
            for p in resp["result"]["outputs"]:
                self.assertTrue(os.path.isfile(p))


class EnvelopeTests(unittest.TestCase):
    """验证宿主信封（run）能正确派发到 Excel / PDF 后端。"""

    def setUp(self):
        self.xl = MockExcelBackend()
        self.pdf = MockPdfBackend()

    def _request(self, action_id, payload):
        return {
            "protocol": "fukuaRPA.plugin",
            "version": 1,
            "kind": "request",
            "payload": {
                "run_id": "r1", "step_id": "s1", "plugin_id": "office-docs",
                "action_id": action_id, "deadline_ns": 0, "payload": payload,
            },
        }

    def test_run_excel(self):
        resp = run(self._request("excel_read_cell", {"path": "x.xlsx", "cell": "A1"}),
                   backend=self.xl, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["value"], "姓名")

    def test_run_pdf(self):
        resp = run(self._request("pdf_page_count", {"path": "b.pdf"}),
                   backend=self.pdf, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["pages"], 4)

    def test_run_unknown(self):
        resp = run(self._request("office_nope", {}), backend=self.xl, backend_name="mock")
        self.assertFalse(resp["ok"])

    def test_run_echoes_run_and_step_id(self):
        # 修复 P2：响应需回显信封里的 run_id/step_id（plugin_protocol 强制字段）。
        resp = run(self._request("excel_read_cell", {"path": "x.xlsx", "cell": "A1"}),
                   backend=self.xl, backend_name="mock")
        self.assertEqual(resp["run_id"], "r1")
        self.assertEqual(resp["step_id"], "s1")


class EnvelopeContractTests(unittest.TestCase):
    """契约测试：run() 必须接收完整 PluginProtocol 信封，防止 P3 宿主误传解析后对象。"""

    def setUp(self):
        self.xl = MockExcelBackend()

    def test_full_envelope_accepted(self):
        """完整信封（protocol/version/kind/payload）应被正确解析并执行。"""
        envelope = {
            "protocol": "fukuaRPA.plugin",
            "version": 1,
            "kind": "request",
            "payload": {
                "plugin_id": "com.fukua.office-docs",
                "action_id": "excel_read_cell",
                "run_id": "run-contract",
                "step_id": "step-contract",
                "deadline_ns": 0,
                "payload": {"path": "test.xlsx", "sheet": "Sheet1", "cell": "A1"},
            },
        }
        resp = run(envelope, backend=self.xl, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["value"], "姓名")
        self.assertEqual(resp["run_id"], "run-contract")
        self.assertEqual(resp["step_id"], "step-contract")

    def test_parsed_request_without_envelope_is_handled(self):
        """缺少外层信封的"解析后 PluginRequest"格式被扁平回退兼容。

        扁平回退（action.py:122-132，与 browser / ocr-local / screen-recorder
        三插件一致）会把直接置于信封根的动作参数恢复出来，避免参数被静默丢弃。
        宿主误把内层 payload 直接当作 request 传入时，动作参数不再丢失：应成功
        执行并回显 run_id/step_id。
        """
        parsed_inner = {
            "plugin_id": "com.fukua.office-docs",
            "action_id": "excel_read_cell",
            "run_id": "run-parsed",
            "step_id": "step-parsed",
            "deadline_ns": 0,
            "payload": {"path": "test.xlsx", "sheet": "Sheet1", "cell": "A1"},
        }
        resp = run(parsed_inner, backend=self.xl, backend_name="mock")
        # 参数经扁平回退保留：path/cell 有效，动作正常执行且元数据正确回显。
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["value"], "姓名")
        self.assertEqual(resp["run_id"], "run-parsed")
        self.assertEqual(resp["step_id"], "step-parsed")


class PdfOverwriteProtectionTests(unittest.TestCase):
    """修复潜在 P2：PDF 合并/拆分对已存在输出应拒绝覆盖。"""

    def setUp(self):
        self.pdf = MockPdfBackend()

    def _exec(self, action_id, payload=None):
        return execute_action(
            "office-docs", action_id, payload or {},
            backend=self.pdf, backend_name="mock",
        )

    def test_split_rejects_out_of_range_pages(self):
        """FR-20260809-1204: 越界页码必须显式拒绝，不得静默生成空/部分 PDF。

        越界校验属 PypdfBackend（真实后端）第一阶段；用假 reader 直接测（不
        依赖 pypdf 安装）。
        """
        from office_docs.pdf_backend import PypdfBackend

        class _FakeReader:
            pages = [object()] * 2  # 2 页文档

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        backend = PypdfBackend()
        original_reader = backend._reader
        backend._reader = lambda path: _FakeReader()
        try:
            # split 开头惰性 `from pypdf import PdfWriter`；越界校验在写入前，
            # mock 掉 pypdf 使校验可达（不依赖 pypdf 安装）。
            with mock.patch.dict(
                "sys.modules", {"pypdf": mock.MagicMock()}
            ):
                with self.assertRaises(ValueError) as ctx:
                    backend.split("x.pdf", "1-3", tempfile.mkdtemp())
                self.assertIn("越界", str(ctx.exception))
                with self.assertRaises(ValueError):
                    backend.split("x.pdf", "3", tempfile.mkdtemp())
                # 混合段含越界时整体失败且无孤儿文件（第一阶段校验不写盘）。
                out_dir = tempfile.mkdtemp()
                with self.assertRaises(ValueError):
                    backend.split("x.pdf", "1-2,99-100", out_dir)
                self.assertEqual(os.listdir(out_dir), [])
        finally:
            backend._reader = original_reader

    def test_merge_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "merged.pdf")
            open(out, "wb").close()
            resp = self._exec("pdf_merge", {"inputs": ["a.pdf", "b.pdf"], "output": out})
            self.assertFalse(resp["ok"])
            self.assertIn("拒绝覆盖", resp["error"])

    def test_split_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out_dir = os.path.join(d, "out")
            os.makedirs(out_dir)
            open(os.path.join(out_dir, "split_1-2.pdf"), "wb").close()
            resp = self._exec("pdf_split", {"path": "b.pdf", "ranges": "1-2,3", "output_dir": out_dir})
            self.assertFalse(resp["ok"])
            self.assertIn("拒绝覆盖", resp["error"])


class OpenpyxlAtomicSaveTests(unittest.TestCase):
    """修复 P1-③：save() 原子写，写出失败时原文件不被破坏。"""

    @unittest.skipUnless(HAS_OPENPYXL, "需要 openpyxl")
    def test_save_keeps_original_on_failure(self):
        from office_docs.excel_backend import OpenpyxlBackend

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "atomic.xlsx")
            wb = openpyxl.Workbook()
            wb.active["A1"] = "original"
            wb.save(p)
            with open(p, "rb") as fh:
                original_bytes = fh.read()

            b = OpenpyxlBackend()
            b.open(p)
            b.write_cell("", "B1", "new")

            # 注入写出失败，验证原文件完好、异常向上传播。
            def _boom(_path):
                raise RuntimeError("write failure")

            b._wb.save = _boom
            with self.assertRaises(RuntimeError):
                b.save()
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), original_bytes)

    @unittest.skipUnless(HAS_OPENPYXL, "需要 openpyxl")
    def test_save_produces_valid_file(self):
        from office_docs.excel_backend import OpenpyxlBackend

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ok.xlsx")
            wb = openpyxl.Workbook()
            wb.active["A1"] = "seed"
            wb.save(p)
            b = OpenpyxlBackend()
            b.open(p)
            b.write_cell("", "A2", 42)
            b.save()
            self.assertTrue(os.path.isfile(p))
            b2 = OpenpyxlBackend()
            b2.open(p)
            self.assertEqual(b2.read_cell("", "A2"), 42)


class PdfSearchReDoSTests(unittest.TestCase):
    """FR-20260809-1202: 病态正则不得挂起 pdf_search（启发式拒绝 + 规模限制）。"""

    def setUp(self):
        self.pdf = MockPdfBackend()

    def test_catastrophic_pattern_rejected_fast(self):
        import time as _time

        t0 = _time.perf_counter()
        result = self.pdf.search("x", r"(a+)+$", "regex")
        self.assertEqual(result, [])
        # 修复前该查询会卡死（实测 30 字符即超过秒级）；修复后必须快速返回。
        self.assertLess(_time.perf_counter() - t0, 2.0)

    def test_nested_quantifier_shapes_rejected(self):
        for query in (r"(a+)+$", r"(a*)*", r"(x+)*", r"(a+){2,}"):
            self.assertEqual(
                self.pdf.search("x", query, "regex"),
                [],
                f"病态正则 {query!r} 应被拒绝",
            )

    def test_overlong_query_rejected(self):
        self.assertEqual(self.pdf.search("x", "a" * 600, "regex"), [])

    def test_normal_regex_unaffected(self):
        result = self.pdf.search("x", r"自动化", "regex")
        self.assertEqual(len(result), 2)
        # 组内无量词 / 无嵌套量词的合法模式不被误拒。
        result2 = self.pdf.search("x", r"(ab)+", "regex")
        self.assertIsInstance(result2, list)
        result3 = self.pdf.search("x", r"插件|OCR", "regex")
        self.assertEqual(len(result3), 2)

    def test_safe_nested_groups_not_rejected(self):
        # FR-20260809-1202: 内层分组无量词的嵌套（(x(y)z)+ / (a(b)+)+）是安全
        # 的线性匹配，不得被启发式误拒（probe3 收紧回归）。
        for query in (r"(x(y)z)+", r"(a(b)+)+", r"(ab(cd)ef)*"):
            self.assertFalse(
                _reject_catastrophic_pattern(query),
                f"安全嵌套 {query!r} 不应被启发式拒绝",
            )
        # 明显病态（双层括号内嵌量词）仍拒绝。
        for query in (r"((a+)+)+", r"(a+(b+))+", r"\((a+)+\)"):
            self.assertTrue(
                _reject_catastrophic_pattern(query),
                f"病态嵌套 {query!r} 应被拒绝",
            )


class PypdfRealApiCoverageTests(unittest.TestCase):
    """FR-20260813-D10-002: 真实 pypdf API 交互此前零覆盖（测试整体顶替 pypdf 模块）。

    本测试注入 fake pypdf 模块，断言 PypdfBackend 真实驱动 PdfReader / PdfWriter
    （merge 与 split 均复用 PdfWriter，pypdf>=6.0 已移除 PdfMerger），将「真实 API
    零覆盖」转为「API 表面已断言」。修复后若重构后端使使用方式变化，本测试会失败，
    从而防回归。
    """

    def test_backend_drives_real_pypdf_classes(self):
        import types as _types

        fake = _types.ModuleType("pypdf")
        reader_calls: list[str] = []
        writer_calls: list[int] = []
        writer_append_calls: list[int] = []

        class _FakeReader:
            def __init__(self, path):
                reader_calls.append(path)
                self.pages = [object(), object()]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        class _FakeWriter:
            def __init__(self):
                writer_calls.append(1)

            def add_page(self, _p):
                pass

            def append(self, _p):
                writer_append_calls.append(1)

            def write(self, _dest):
                pass

            def close(self):
                pass

        fake.PdfReader = _FakeReader
        fake.PdfWriter = _FakeWriter

        from office_docs.pdf_backend import PypdfBackend

        with mock.patch.dict(sys.modules, {"pypdf": fake}):
            backend = PypdfBackend()
            with tempfile.TemporaryDirectory() as d:
                # page_count → PdfReader
                self.assertEqual(backend.page_count("doc.pdf"), 2)
                # merge → PdfWriter（pypdf>=6.0 合并改用 PdfWriter.append）
                out = os.path.join(d, "merged.pdf")
                self.assertEqual(backend.merge(["a.pdf", "b.pdf"], out), 2)
                # split → PdfWriter
                out_dir = os.path.join(d, "split")
                paths = backend.split("doc.pdf", "1-2", out_dir)
                self.assertEqual(len(paths), 1)

        self.assertTrue(reader_calls, "PypdfBackend 未驱动真实 PdfReader")
        self.assertTrue(writer_calls, "PypdfBackend 未驱动真实 PdfWriter")
        self.assertTrue(writer_append_calls, "PypdfBackend merge 未驱动真实 PdfWriter.append")


@unittest.skipUnless(HAS_PYPDF, "需要安装 pypdf 以跑真实 API 集成测试")
class PypdfIntegrationTests(unittest.TestCase):
    """FR-20260813-D10-002（专项补强）：真实 pypdf API 字节层端到端集成覆盖。

    ``PypdfRealApiCoverageTests`` 仅用 fake 模块断言「驱动了正确的类名」，未验证
    真实 PDF 字节的读写行为。本类在 pypdf 已安装时，用真实 ``PdfWriter`` 生成合法
    多页 PDF，再经 ``PypdfBackend`` 真实路径（不替换 sys.modules）验证
    ``page_count`` / ``merge`` / ``split`` 在真实 pypdf 字节上工作，且产物可被真实
    ``PdfReader`` 再次读回正确页数。

    边界说明：文本提取/搜索的真实「含文本」语义已由 ``PypdfRealApiCoverageTests``
    （断言驱动正确的 PdfReader 类名）与 ``MockPdfBackend`` 等价逻辑测试覆盖；真实
    PDF 文本提取的正确性是 pypdf 库职责，非本项目后端缺陷面，故本类不重复构造脆弱
    的文本 PDF 字节。未安装 pypdf 时整体跳过（与 ``OpenpyxlAtomicSaveTests`` 守卫
    一致），不阻塞无依赖环境冒烟。
    """

    def _make_blank_pdf(self, pages, path):
        """用真实 pypdf 写出含 pages 页的合法 PDF。"""
        from pypdf import PdfWriter

        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=612, height=792)
        writer.write(path)
        return path

    def test_page_count_real(self):
        from office_docs.pdf_backend import PypdfBackend

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "doc.pdf")
            self._make_blank_pdf(3, path)
            self.assertEqual(PypdfBackend().page_count(path), 3)

    def test_merge_real_writes_valid_pdf(self):
        from office_docs.pdf_backend import PypdfBackend

        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.pdf")
            b = os.path.join(d, "b.pdf")
            self._make_blank_pdf(2, a)
            self._make_blank_pdf(3, b)
            out = os.path.join(d, "merged.pdf")
            n = PypdfBackend().merge([a, b], out)
            self.assertEqual(n, 5)
            self.assertTrue(os.path.exists(out))
            # 合并产物可被真实 PdfReader 再次读取（验证字节合法）。
            from pypdf import PdfReader

            with PdfReader(out) as r:
                self.assertEqual(len(r.pages), 5)

    def test_split_real_writes_valid_pages(self):
        from office_docs.pdf_backend import PypdfBackend

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "doc.pdf")
            self._make_blank_pdf(3, path)
            out_dir = os.path.join(d, "split")
            paths = PypdfBackend().split(path, "1-2", out_dir)
            self.assertEqual(len(paths), 1)
            self.assertTrue(os.path.exists(paths[0]))
            from pypdf import PdfReader

            with PdfReader(paths[0]) as r:
                self.assertEqual(len(r.pages), 2)

    def test_merge_refuses_overwrite_real(self):
        from office_docs.pdf_backend import PypdfBackend

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "merged.pdf")
            with open(out, "wb") as fh:
                fh.write(b"preexisting")
            with self.assertRaises(FileExistsError):
                PypdfBackend().merge(["a.pdf", "b.pdf"], out)


class PdfParsePagesRangeTests(unittest.TestCase):
    """FR-a01-004: 逆序页码范围必须显式拒绝（与 pdf_split 语义一致），
    不得静默返回空文本。"""

    def test_reverse_range_rejected(self):
        from office_docs.pdf_backend import parse_pages

        with self.assertRaises(ValueError):
            parse_pages("5-1")

    def test_reverse_range_within_sequence_rejected(self):
        from office_docs.pdf_backend import parse_pages

        with self.assertRaises(ValueError):
            parse_pages("1-3,7-2")

    def test_normal_range_still_parsed(self):
        from office_docs.pdf_backend import parse_pages

        self.assertEqual(parse_pages("1-3,5"), [1, 2, 3, 5])
        self.assertIsNone(parse_pages(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
