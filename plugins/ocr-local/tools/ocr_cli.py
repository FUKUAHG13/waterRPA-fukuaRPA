#!/usr/bin/env python3
"""本地 OCR 插件 —— 独立模式命令行工具（不依赖 fukuaRPA 宿主）。

现在就能对一张本地图片做离线 OCR / 找词 / 本地点击，用于开发调试与独立使用。
fukuaRPA 集成模式的点击由宿主执行；本工具的本地点击仅在 Windows 上可用。

用法
----
    # 找词并输出坐标（默认后端 rapidocr，需先 pip install -r requirements.txt）
    python tools/ocr_cli.py shot.png --target 确定

    # 仅读取区域全部文字
    python tools/ocr_cli.py shot.png --read

    # 多候选，按列表顺序取第一个命中
    python tools/ocr_cli.py shot.png --targets 登录 注册 --match-mode equals

    # 用测试后端（无需安装 rapidocr）跑通逻辑
    python tools/ocr_cli.py shot.png --target 确定 --backend mock

    # 找到后本地点击（仅 Windows）
    python tools/ocr_cli.py shot.png --target 确定 --click

退出码：找到目标 0；未找到 / 出错 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 让 tools/ 能 import 到 src/ocr_local
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ocr_local.action import execute_action  # noqa: E402
from ocr_local.backend import MockBackend, create_backend  # noqa: E402


def _make_backend(name: str):
    if name == "mock":
        return MockBackend()
    try:
        return create_backend(name)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法加载后端 {name!r}（{exc}）。请先执行：")
        print("       pip install -r requirements.txt")
        print("       或改用 --backend mock 跑通逻辑。")
        raise SystemExit(1) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 OCR 插件独立 CLI")
    parser.add_argument("image", help="待识别的图片路径（PNG/JPG/BMP）")
    parser.add_argument("--target", help="查找的单个目标文字")
    parser.add_argument("--targets", nargs="+", help="查找的候选文字列表（顺序优先）")
    parser.add_argument("--match-mode", default="contains",
                        choices=["contains", "equals", "regex"], help="匹配方式")
    parser.add_argument("--score-threshold", type=float, default=0.5, help="置信度阈值 0-1")
    parser.add_argument("--backend", default="rapidocr",
                        choices=["rapidocr", "mock"], help="OCR 后端")
    parser.add_argument("--read", action="store_true", help="仅读取区域文字，不查找目标")
    parser.add_argument("--click", action="store_true", help="命中后本地点击（仅 Windows）")
    parser.add_argument(
        "--origin",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="图片在屏幕上的区域原点（屏幕坐标）；--click 时必须提供，"
        "用于把图像局部坐标叠加为屏幕物理坐标（FR-20260812-OCR-4）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"[error] 图片不存在：{args.image}")
        return 1

    backend = _make_backend(args.backend)

    payload = {
        "image_path": args.image,
        "match_mode": args.match_mode,
        "score_threshold": args.score_threshold,
    }
    if args.targets:
        payload["targets"] = args.targets
    if args.target:
        payload["target_text"] = args.target

    if args.read:
        resp = execute_action("ocr-local", "ocr_read_region", payload, backend=backend)
    else:
        resp = execute_action("ocr-local", "ocr_find_click", payload, backend=backend)

    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        if not resp.get("ok", False):
            return 1
        result = resp.get("result") or {}
        if not args.read and not result.get("found", False):
            return 1
        return 0
    else:
        if not resp["ok"]:
            print(f"[error] {resp.get('error')}")
            return 1
        result = resp["result"]
        if args.read:
            print(f"识别到文字：{result.get('found')}")
            print(result.get("text", ""))
        else:
            if result.get("found"):
                print(f"命中：{result.get('matched_text')} "
                      f"@ ({result.get('click_x')}, {result.get('click_y')}) "
                      f"score={result.get('score')}")
                if args.click:
                    # FR-20260812-OCR-4: 图像局部坐标不能直接当屏幕坐标点击。
                    # 必须提供 --origin 叠加区域原点；未提供则拒绝点击并说明。
                    if args.origin is None:
                        print(
                            "[error] --click 需要 --origin X Y（图片在屏幕上的区域原点）；"
                            "命中坐标是图像局部坐标，不能直接当屏幕坐标使用。"
                        )
                        return 1
                    from ocr_local.input_click import click_at

                    screen_x = float(result["click_x"]) + args.origin[0]
                    screen_y = float(result["click_y"]) + args.origin[1]
                    click_at(screen_x, screen_y)
                    print("[ok] 已本地点击")
            else:
                print("未找到目标文字。")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
