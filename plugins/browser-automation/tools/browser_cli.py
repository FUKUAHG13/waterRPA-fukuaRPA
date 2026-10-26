#!/usr/bin/env python3
"""浏览器自动化插件 —— 独立模式命令行工具（不依赖 fukuaRPA 宿主）。

直接驱动 Playwright 对网页做 DOM 级操作，用于开发调试与独立使用。
fukuaRPA 集成模式下同样由本插件的 action.run 执行，只是浏览器由插件管理。

用法
----
    # 导航并打印落地地址（需先 pip install playwright && playwright install）
    python tools/browser_cli.py navigate --url https://example.com

    # 点击页面上文字为"登录"的按钮
    python tools/browser_cli.py click --by text --value 登录

    # 填写输入框
    python tools/browser_cli.py fill --by css --value "input[name=username]" --text alice

    # 提取整页文本
    python tools/browser_cli.py extract

    # 用测试后端（无需安装 playwright）跑通逻辑
    python tools/browser_cli.py extract --backend mock

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

from browser_automation.action import execute_action  # noqa: E402
from browser_automation.backend import MockBrowserBackend, create_backend  # noqa: E402


def _make_backend(name: str):
    if name == "mock":
        return MockBrowserBackend(), True  # mock 不需要持久浏览器，auto_close
    try:
        return create_backend(name), True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法加载后端 {name!r}（{exc}）。请先执行：")
        print("       pip install playwright && playwright install")
        print("       或改用 --backend mock 跑通逻辑。")
        raise SystemExit(1) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器自动化插件独立 CLI")
    parser.add_argument(
        "action",
        choices=["navigate", "click", "fill", "extract", "wait", "attr", "shot"],
    )
    parser.add_argument("--url", help="navigate 用：目标网址")
    parser.add_argument("--by", default="text", choices=["text", "css", "xpath"])
    parser.add_argument("--value", help="定位文本或选择器")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--text", help="fill 用：要填入的文本")
    parser.add_argument("--attr", help="attr 用：属性名")
    parser.add_argument("--timeout", type=int, default=5000)
    parser.add_argument("--path", help="shot 用：截图保存路径")
    parser.add_argument("--backend", default="playwright", choices=["playwright", "mock"])
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    backend, auto_close = _make_backend(args.backend)

    payload: dict = {}
    action_id = {
        "navigate": "browser_navigate",
        "click": "browser_click",
        "fill": "browser_fill",
        "extract": "browser_extract_text",
        "wait": "browser_wait_for",
        "attr": "browser_get_attribute",
        "shot": "browser_screenshot",
    }[args.action]

    if args.action == "navigate":
        payload["url"] = args.url
    if args.action in ("click", "fill", "wait", "attr"):
        payload["by"] = args.by
        payload["value"] = args.value
        payload["index"] = args.index
    if args.action == "fill":
        payload["text"] = args.text
    if args.action == "attr":
        payload["attr"] = args.attr
    if args.action == "wait":
        payload["timeout_ms"] = args.timeout
    if args.action == "extract" and args.value:
        payload["by"] = args.by
        payload["value"] = args.value
    if args.action == "shot":
        payload["path"] = args.path or "browser_shot.png"
        if args.value:
            payload["by"] = args.by
            payload["value"] = args.value

    resp = execute_action(
        "browser-automation", action_id, payload,
        backend=backend, backend_name=args.backend, auto_close=auto_close,
    )

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        if not resp.get("ok", False):
            return 1
    else:
        if not resp["ok"]:
            print(f"[error] {resp.get('error')}")
            return 1
        result = resp["result"]
        print(f"动作：{result.get('action')}  found={result.get('found')}")
        if "url" in result:
            print(f"URL：{result['url']}")
        if "text" in result:
            print("文本：")
            print(result["text"])
        if "value" in result:
            print(f"属性值：{result['value']}")
        if "element" in result:
            el = result["element"]
            print(f"元素：{el['selector']} 文本={el['text']!r} 包围盒={el['bounding_box']}")
        if "path" in result:
            print(f"截图：{result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
