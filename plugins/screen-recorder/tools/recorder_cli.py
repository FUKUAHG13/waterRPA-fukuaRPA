#!/usr/bin/env python3
"""屏幕录制插件 —— 独立模式命令行工具（不依赖 fukuaRPA 宿主）。

直接驱动真实后端（mss/ffmpeg）或测试后端（mock）做录屏/截图，用于开发调试与
独立使用。fukuaRPA 集成模式下同样由本插件的 action.run 执行，只是后端由宿主管理。

用法
----
    # 开始录制到指定文件（默认 mss 后端，需先 pip install mss opencv-python）
    python tools/recorder_cli.py start --path rec.mp4 --fps 15

    # 暂停 / 恢复 / 停止（注意：这几步需在「同一进程」内连续执行才有状态）
    python tools/recorder_cli.py pause
    python tools/recorder_cli.py resume
    python tools/recorder_cli.py stop

    # 单帧截图
    python tools/recorder_cli.py shot --path shot.png

    # 用测试后端（无需安装任何第三方库）跑通逻辑
    python tools/recorder_cli.py start --path rec.mp4 --backend mock

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

from screen_recorder.action import execute_action  # noqa: E402
from screen_recorder.backend import MockRecorderBackend, create_backend  # noqa: E402


def _make_backend(name: str):
    if name == "mock":
        # mock 会话是单步无状态逻辑验证，auto_close 即可。
        return MockRecorderBackend(), True
    try:
        return create_backend(name), True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法加载后端 {name!r}（{exc}）。")
        if name == "mss":
            print("       请先执行：pip install mss opencv-python")
        elif name == "ffmpeg":
            print("       请安装 ffmpeg 并加入系统 PATH")
        print("       或改用 --backend mock 跑通逻辑。")
        raise SystemExit(1) from None


def _parse_region(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        x, y, w, h = (int(v) for v in text.split(","))
    except ValueError:
        print("[error] region 需为 x,y,width,height（逗号分隔的整数）")
        raise SystemExit(1) from None
    return {"x": x, "y": y, "width": w, "height": h}


def main() -> int:
    parser = argparse.ArgumentParser(description="屏幕录制插件独立 CLI")
    parser.add_argument(
        "action", choices=["start", "pause", "resume", "stop", "shot"],
    )
    parser.add_argument("--path", help="start/shot 用：输出文件路径（.mp4/.png）")
    parser.add_argument("--output-dir", help="输出目录（自动命名）")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--format", default="mp4", choices=["mp4", "avi"])
    parser.add_argument("--region", help="可选录制/截图区域 x,y,width,height")
    parser.add_argument("--audio", action="store_true", help="start 用：录制系统音轨（ffmpeg 后端）")
    parser.add_argument("--backend", default="mss", choices=["mss", "ffmpeg", "mock"])
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    backend, auto_close = _make_backend(args.backend)
    region = _parse_region(args.region)

    action_id = {
        "start": "recorder_start",
        "pause": "recorder_pause",
        "resume": "recorder_resume",
        "stop": "recorder_stop",
        "shot": "recorder_snapshot",
    }[args.action]

    payload: dict = {}
    if args.action == "start":
        if args.path:
            payload["output_path"] = args.path
        elif args.output_dir:
            payload["output_dir"] = args.output_dir
            payload["format"] = args.format
        else:
            print("[error] start 需要 --path 或 --output-dir")
            return 1
        payload["fps"] = args.fps
        payload["audio"] = args.audio
        if region:
            payload["region"] = region
    elif args.action == "shot":
        if args.path:
            payload["path"] = args.path
        elif args.output_dir:
            payload["output_dir"] = args.output_dir
        else:
            print("[error] shot 需要 --path 或 --output-dir")
            return 1
        if region:
            payload["region"] = region

    resp = execute_action(
        "screen-recorder", action_id, payload,
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
        print(f"动作：{result.get('action')}  recording={result.get('recording')}  paused={result.get('paused')}")
        if "output_path" in result and result["output_path"]:
            print(f"视频：{result['output_path']}")
        if "path" in result and result["path"]:
            print(f"截图：{result['path']}")
        if "duration_ms" in result:
            print(f"时长：{result['duration_ms']} ms  帧数：{result.get('frames')}  大小：{result.get('file_bytes')} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
