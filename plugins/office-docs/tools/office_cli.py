#!/usr/bin/env python3
"""办公文档插件 —— 独立模式命令行工具（不依赖 fukuaRPA 宿主）。

直接对本地 Excel/PDF 做读写、查找、合并、拆分，用于开发调试与独立使用。
fukuaRPA 集成模式下同样由本插件的 action.run 执行。

用法
----
    # 读取单元格
    python tools/office_cli.py --action excel_read_cell \
        --payload '{"path":"a.xlsx","sheet":"Sheet1","cell":"A1"}'

    # 用测试后端（无需 openpyxl/pypdf）跑通逻辑
    python tools/office_cli.py --action excel_find \
        --payload '{"path":"a.xlsx","value":"Alice"}' --backend mock

    # PDF 搜索
    python tools/office_cli.py --action pdf_search \
        --payload '{"path":"b.pdf","query":"自动化"}' --backend mock

退出码：成功 0；失败 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from office_docs.action import execute_action  # noqa: E402
from office_docs.excel_backend import MockExcelBackend, create_excel_backend  # noqa: E402
from office_docs.pdf_backend import MockPdfBackend, create_pdf_backend  # noqa: E402


def _make_backend(action_id: str, name: str | None):
    if name is None:
        name = "openpyxl" if action_id.startswith("excel_") else "pypdf"
    if name == "mock":
        if action_id.startswith("excel_"):
            return MockExcelBackend()
        return MockPdfBackend()
    try:
        if action_id.startswith("excel_"):
            return create_excel_backend(name)
        return create_pdf_backend(name)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法加载后端 {name!r}（{exc}）。请先执行：")
        print("       pip install -r requirements.txt")
        print("       或改用 --backend mock 跑通逻辑。")
        raise SystemExit(1) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="办公文档插件独立 CLI")
    parser.add_argument("--action", required=True,
                        help="动作 id，如 excel_read_cell / pdf_search")
    parser.add_argument("--payload", required=True, help="JSON 参数字符串")
    parser.add_argument("--backend", default=None,
                        help="后端名（excel: openpyxl/mock；pdf: pypdf/mock；默认按动作自动选择）")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        print(f"[error] payload 不是合法 JSON：{exc}")
        return 1

    backend_name = args.backend if args.backend else (
        "openpyxl" if args.action.startswith("excel_") else "pypdf"
    )
    backend = _make_backend(args.action, args.backend)
    # 已构造好后端实例并传入；execute_action 会直接使用，不再自建。
    resp = execute_action(
        "office-docs", args.action, payload,
        backend=backend, backend_name=backend_name,
    )

    if not resp["ok"]:
        print(f"[error] {resp.get('error')}")
        return 1

    result = resp["result"]
    print(f"动作：{result.get('action')}")
    for key in ("value", "values", "text", "addresses", "sheets", "rows",
                "pages", "matches", "outputs", "written"):
        if key in result:
            print(f"{key}：{result[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
