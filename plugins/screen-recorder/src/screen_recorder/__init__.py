"""屏幕录制 / 视频取证插件（fukuaRPA 插件契约实现）。

提供「屏幕录制」「暂停/恢复」「停止取证」「单帧截图」五类动作。
默认真实后端为 MssOpencvBackend（mss 抓屏 + opencv 编码，纯离线、可控帧率，
支持暂停/恢复）；另提供 FfmpegBackend（系统 ffmpeg gdigrab，画质更高，但暂停/
恢复受 ffmpeg 限制暂不支持）作为可选后端；以及 MockRecorderBackend 用于无依赖
测试。

权限模型（见 manifest.json）：
* screen.capture_region —— 抓屏/录屏核心能力。
* files.user_selected —— 用户指定输出文件路径。
* files.plugin_private_data —— 未指定路径时写入插件私有目录。
* 本插件**不声明任何网络权限**：录屏为完全本地行为，符合 fukuaRPA「不联网」。
"""

from __future__ import annotations

from .action import execute_action, run

__all__ = ["execute_action", "run"]
__version__ = "1.1.0"
