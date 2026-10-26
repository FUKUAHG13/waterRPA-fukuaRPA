# 屏幕录制与视频取证（screen-recorder）

fukuaRPA 的**可选插件**之一，为 RPA 流程补充「屏幕录制 / 单帧截图 / 视频取证归档」
能力。与 OCR、浏览器自动化、办公文档三款插件一样，**独立文件夹、零改动原项目**，
复用同一套 fukuaRPA v1 插件契约。

## 为什么做成插件

屏幕录制涉及视频编码（mss+opencv 或 ffmpeg），对体积与运行环境（第三方库 /
系统二进制）影响较大。把它做成**选装插件**，让不需要取证录屏的用户不被这些依赖
拖累，需要的人再安装即可——这正是「按体积影响选装」的思路。

## 功能与动作

| 动作 id | 名称 | 说明 |
| --- | --- | --- |
| `recorder_start` | 开始屏幕录制 | 可选区域/帧率/音轨；默认 mss+opencv 软编码，纯离线、支持暂停/恢复 |
| `recorder_pause` | 暂停录制 | 仅 mss 后端支持 |
| `recorder_resume` | 恢复录制 | 仅 mss 后端支持 |
| `recorder_stop` | 停止并取证 | 返回输出路径、时长、帧数、文件大小，便于归档 |
| `recorder_snapshot` | 单帧截图 | 抓当前屏幕（可选区域）存 PNG，独立于录制会话，常用于取证留证 |

## 后端

| 后端 | 默认 | 依赖 | 暂停/恢复 | 说明 |
| --- | --- | --- | --- | --- |
| `mss` | ✅ 真实默认 | `mss` + `opencv-python` | ✅ 支持 | Python 层精确控帧，纯离线，无需系统 ffmpeg |
| `ffmpeg` | 可选 | 系统 `ffmpeg`（PATH） | ❌ 不支持 | gdigrab 高质量录屏；暂停/恢复受外部程序限制，调用会明确报错 |
| `mock` | 测试用 | 无 | ✅ 支持 | 状态机 + 占位文件，用于单元测试与无依赖逻辑冒烟 |

> 默认真实后端为 **mss**（mss 抓屏 + opencv 软编码）。若你的场景需要更高画质且
> 不在乎暂停/恢复，可在宿主注入 `backend_name="ffmpeg"` 并自行安装 ffmpeg。

## 权限（manifest 声明）

- `screen.capture_region` —— 抓屏/录屏核心能力
- `files.user_selected` —— 用户指定输出路径
- `files.plugin_private_data` —— 未指定路径时写入插件私有目录

**本插件不声明任何网络权限**。录屏/截图是纯本地取证行为，不涉及任何对外网络通信，
符合 fukuaRPA 核心「不联网」承诺（详见 `AGENTS.md` 兼容性章节）。

## 信任边界

屏幕录制/截图完全在本地完成，不向本机之外的任何地址发起请求、不发送遥测。本插件
不会引入对外通信路径，与「fukuaRPA 不联网」的承诺一致。

## 隐私提示

录屏和截图产出文件包含屏幕内容，可能涉及敏感信息（密码输入、私人对话、文档内容等）。建议：

- 将输出目录设置为仅当前用户可访问的路径（避免共享目录）
- 录制完成后及时清理或加密存储产出文件
- 不要在录制期间输入密码或显示敏感信息
- 分享录制文件前确认内容不含隐私数据

## 安装运行时依赖（真实后端）

```bash
pip install mss opencv-python      # mss 后端（默认）
# 或：自行安装 ffmpeg 并加入 PATH   # ffmpeg 后端（可选）
```

## 开发与验证

```bash
# 1) 单元测试（Mock 后端，无需任何第三方库）
python tests/test_screen_recorder.py

# 2) 生成 manifest.json + 打包 .fkrplugin，并用原项目契约实跑校验
python tools/build_plugin.py

# 3) 独立 CLI 冒烟（mock 后端）
# 注意：每次 CLI 调用都是独立进程，start 创建的录制会话不会跨进程保留，
# 因此 start 与 stop 不能分成两次 CLI 调用（第二次会因找不到会话而失败）。
# mock 会话是单步无状态逻辑验证，下列单步动作可各自独立执行：
python tools/recorder_cli.py start --path /tmp/rec.mp4 --backend mock
python tools/recorder_cli.py shot  --path /tmp/shot.png --backend mock

# 真实后端（需先装 mss + opencv-python）：start→pause→resume→stop 的完整
# 生命周期必须在「同一进程」内连续执行才有状态（工具 docstring 同声明）；
# 独立 CLI 仅用于验证单个动作，例如启动录制：
python tools/recorder_cli.py start --path rec.mp4 --fps 15
```

## 目录结构

```
screen-recorder/
├── manifest.json            # 由 build_plugin.py 生成（含逐文件 SHA-256）
├── screen-recorder.fkrplugin# 可分发包（ZIP：manifest + 运行时文件）
├── requirements.txt
├── README.md
├── LICENSE
├── src/screen_recorder/     # 运行时源码（进包）
│   ├── __init__.py
│   ├── action.py            # 宿主入口 run / execute_action
│   ├── backend.py           # mss / ffmpeg / mock 三种后端
│   ├── engine.py            # 动作编排（不依赖宿主）
│   └── models.py            # 状态/结果数据模型
├── tools/                   # 构建脚本 + 独立 CLI（不进包）
│   ├── build_plugin.py
│   └── recorder_cli.py
└── tests/                   # 单元测试（不进包）
    └── test_screen_recorder.py
```

## 契约约束

- 符合 `fukuaRPA.plugin` 格式、manifest_version=1、api_version=1。
- `entrypoint` 为字符串 `src/screen_recorder/action.py:run`。
- `manifest.dependencies` 仅用于「插件间依赖」（需为插件 id），pip 运行时依赖写在
  `requirements.txt`，不在此声明——否则原项目 `PluginManifest.parse_json` 会拒绝。
- 打包单文件 ≤16MiB、总 ≤256MiB；`.fkrplugin` 仅含运行时文件，不含 `__pycache__`。
