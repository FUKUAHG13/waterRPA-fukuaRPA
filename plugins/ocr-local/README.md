# 本地 OCR 插件（离线文字识别）

一个为 [fukuaRPA](https://github.com/FUKUAHG13/waterRPA-fukuaRPA) 设计的**选装插件**：
用离线 OCR 让 RPA 流程「认得屏幕上的字」，把"找到写着『确定』的按钮并点它"变成可能。

> 当前 fukuaRPA 主程序只做**模板图片匹配**，不认文字。OCR 正好补上这个最大的能力缺口。

## 为什么做成插件（而不是塞进主程序）

- **体积**：默认 OCR 后端 `rapidocr-onnxruntime` 含 onnxruntime 与模型，体积可观。
  做成插件后，主程序永不背这个负担——需要的人选装即可。
- **可选装**：与主程序 `plugins/` 插件系统（manifest + 宿主进程 + 逐文件完整性校验）天然契合。
- **零改动**：本插件完全放在独立文件夹 `plugins/ocr-local/`，**不修改 fukuaRPA 原项目任何一行代码**。

## 离线承诺（不联网）

本插件**不申请任何网络权限**。OCR 推理全部在本地完成，不会发起对外网络请求、
不上传截图、不发送遥测——与 fukuaRPA「本程序不联网」的产品边界一致。

manifest 中声明的权限只有三类（均为本机能力）：

| 权限 | 用途 |
| --- | --- |
| `screen.capture_region` | 截取指定区域（P3 宿主注入 `image_b64`） |
| `input.mouse` | 命中目标文字后由宿主执行点击（保证可被急停取消、停止后不残留输入） |
| `variables.write_declared` | 把命中文字写入用户声明过的变量 |

## 目录结构

```
plugins/ocr-local/
├── manifest.json          # 插件清单（由 build_plugin.py 生成，含逐文件 SHA-256）
├── requirements.txt       # 运行依赖（numpy / Pillow / rapidocr-onnxruntime）
├── README.md
├── LICENSE
├── src/ocr_local/         # 插件运行时（可被 fukuaRPA 宿主加载）
│   ├── action.py          # 动作入口：run(request) → PluginResponse 形状
│   ├── backend.py         # OCR 后端抽象 + RapidOCR 实现 + Mock（测试用）
│   ├── engine.py          # 阈值过滤与结构化
│   ├── io_image.py        # 路径 / base64 → numpy(BGR)
│   ├── matcher.py         # 目标文本匹配与点击坐标计算
│   ├── models.py          # OcrWord / OcrResult 数据模型
│   └── input_click.py     # 独立模式本地点击（仅 Windows；集成模式由宿主点击）
├── tools/
│   ├── build_plugin.py    # 生成 manifest.json 并打包成 .fkrplugin
│   └── ocr_cli.py         # 独立模式命令行工具（不依赖宿主）
└── tests/
    └── test_ocr_local.py  # 单元测试
```

## 两个动作

1. **`ocr_find_click` —— OCR 识别文字并点击**
   对区域做文字识别，找到目标文本后返回其屏幕坐标，由宿主点击。
   关键参数：`target_text` / `targets`（候选列表）、`match_mode`
   （`contains`/`equals`/`regex`）、`score_threshold`、`click_offset`、`set_variable`。
2. **`ocr_read_region` —— OCR 读取区域文字**
   识别区域全部文字并返回文本与坐标，不点击。

## 两种使用模式

### 模式 A：独立使用（现在就能跑）

```bash
pip install -r requirements.txt

# 找词并输出坐标（默认 rapidocr 后端）
python tools/ocr_cli.py shot.png --target 确定

# 仅读取区域文字
python tools/ocr_cli.py shot.png --read

# 多候选，按列表顺序取第一个命中
python tools/ocr_cli.py shot.png --targets 登录 注册 --match-mode equals

# 不装 heavy 依赖也能跑通逻辑（测试后端）
python tools/ocr_cli.py shot.png --target 确定 --backend mock

# 命中后本地点击（仅 Windows）
python tools/ocr_cli.py shot.png --target 确定 --click
```

也可以直接在代码里 `import ocr_local` 复用 `execute_action(...)` / `ocr_image(...)`。

### 模式 B：作为 fukuaRPA 插件（待 P3 宿主落地）

1. `python tools/build_plugin.py` 生成 `manifest.json` 与 `ocr-local.fkrplugin`。
2. 把 `ocr-local.fkrplugin` 交给 fukuaRPA 的插件中心安装
   （安装时会做路径穿越 / 符号链接 / 文件大小 / 逐文件 SHA-256 等安全校验）。
3. 在步骤里选择「插件动作 → ocr-local / ocr_find_click（或 ocr_read_region）」，
   在 `plugin_config` 里填写参数；运行到该步骤时由宿主调用
   `src/ocr_local/action.py:run(request)`。

> 当前 fukuaRPA 的插件宿主（P2 阶段）是**校验桩**，仅验证调用链而不真正执行第三方代码；
> 真实执行在 P3 的隔离运行时。本插件已按契约写好 `manifest.json` 与 `run` 入口，
> P3 落地后即可无缝接入，**无需改动本插件**。

## 隐私与安全

- OCR 识别出的文字属于敏感内容；本插件**不主动写日志**。在 fukuaRPA 集成模式下，
  动作日志由宿主按既有隐私策略处理（凭据 / 秘密文本永远不进日志）。
- 插件包安装经过严格完整性校验，逐文件 SHA-256 与路径安全扫描，杜绝被篡改或注入。
- 默认后端模型随 pip 包安装，安装后纯本地推理，无网络行为。

## 构建与测试

```bash
# 生成清单 + 打包（会尝试用原项目契约做一次真实校验）
python tools/build_plugin.py

# 仅生成 manifest.json
python tools/build_plugin.py --no-zip

# 仅用原项目契约校验已有 manifest.json
python tools/build_plugin.py --validate

# 运行单元测试
python -m pytest tests/test_ocr_local.py -v
# 或直接：
python tests/test_ocr_local.py
```

测试覆盖：数据模型几何、文本匹配（contains/equals/regex）、动作入口与返回形状、
未提供图像 / 未知动作等边界、引擎置信度阈值过滤。纯逻辑部分无需安装重依赖即可运行；
阈值测试在 `numpy` 可用时额外执行。

## 已知约束 / 路线图

- P3 宿主未实现前，fukuaRPA 内不能真正执行本插件（仅能被校验/列出）。
- 默认中文 + 英文；如需更多语种，可接入 PaddleOCR 等其它本地后端
  （在 `backend.py` 的 `_BACKEND_REGISTRY` 注册即可，接口不变）。
- 当前 `plugin_type` 填 `python_action` 以表明「提供可执行动作」，与四个插件
  manifest 的实际取值一致。

## 与原项目的关系

本目录为**派生开发产物**，独立于 fukuaRPA 主线源码。它只复用 fukuaRPA 公开的
插件契约（`plugin_manifest.py` / `plugin_protocol.py` / `plugin_package.py` 定义的格式），
不修改、不依赖其运行时内部实现。
