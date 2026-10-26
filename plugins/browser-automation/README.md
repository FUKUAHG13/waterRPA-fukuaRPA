# 浏览器自动化插件（browser-automation）

fukuaRPA 的**选装插件**之一，提供网页 DOM 级自动化能力：导航、点击、填表、
提取文本、等待元素、读取属性、截图。基础程序保持轻量，**默认不带浏览器**，
需要的人再装本插件。

> 本插件与「本地 OCR 插件」同属第二层选装插件，均遵循 `fukuaRPA.plugin` 插件契约。

## 信任边界（重要）

fukuaRPA 核心承诺**永远不联网**。本插件不突破该承诺：

- 本插件**不声明任何网络权限**（v1 权限枚举也无 `network` 项）。
- 浏览器联网行为来自**用户主动选装并启动的浏览器子进程**（通过
  `process.launch_confirmed` 权限显式声明）。这与"fukuaRPA 主程序不对外通信"
  是两个层面：主程序仍离线，浏览器是用户自己控制的独立进程。
- 若你不需要网页自动化，不装本插件即可，主程序体积与行为不受影响。

## 提供的动作

| 动作 id | 作用 | 关键参数 |
|---|---|---|
| `browser_navigate` | 导航到网址 | `url`, `wait_until`, `headless`, `timeout_ms` |
| `browser_click` | 点击元素 | `by`(text/css/xpath), `value`, `index` |
| `browser_fill` | 填写输入框 | `by`, `value`, `text`, `index` |
| `browser_extract_text` | 提取文本 | `by`, `value`(可选，取整页或元素) |
| `browser_wait_for` | 等待元素出现 | `by`, `value`, `timeout_ms` |
| `browser_get_attribute` | 读取属性 | `by`, `value`, `attr`, `index` |
| `browser_screenshot` | 网页截图 | `path`, `by`, `value`(可选) |

定位方式 `by`：`text`（按可见文本）、`css`（CSS 选择器）、`xpath`（XPath）。

## 目录结构

```
browser-automation/
├── manifest.json              # 插件清单（由 build_plugin.py 生成）
├── src/browser_automation/    # 运行时源码
│   ├── action.py              # 宿主入口 run() / execute_action()
│   ├── backend.py             # Playwright 后端 + Mock 后端
│   ├── engine.py              # 动作编排
│   └── models.py              # 数据模型
├── tools/
│   ├── build_plugin.py        # 生成清单 + 打包 + 原项目契约校验
│   └── browser_cli.py         # 独立命令行工具（现在就能用）
├── tests/test_browser_automation.py
├── requirements.txt
├── LICENSE
└── README.md
```

## 使用方式

### 1. 独立模式（现在就能用）

```bash
pip install -r requirements.txt
playwright install            # 下载浏览器二进制
python tools/browser_cli.py navigate --url https://example.com
python tools/browser_cli.py click --by text --value 登录
python tools/browser_cli.py extract --backend mock   # 无需浏览器，验证逻辑
```

### 2. fukuaRPA 插件模式

待宿主 P3 运行时落地后，用 `tools/build_plugin.py` 生成 `browser-automation.fkrplugin`，
在 fukuaRPA 中安装即可在步骤里选用上述动作。宿主会按 `manifest.json` 的
`entrypoint`（`src/browser_automation/action.py:run`）加载并执行。

## 验证

```bash
python tests/test_browser_automation.py     # 单元测试（Mock 后端，无需浏览器）
python tools/build_plugin.py --validate     # 用原项目契约校验 manifest
```

## 体积说明

- 本插件代码本身很小（纯 Python）。
- 真正占体积的是 **Playwright + 浏览器二进制（~150MB+）**，只在用户选装并
  `pip install`/`playwright install` 时才下载，不会进入 fukuaRPA 主程序。
- 这正是做成插件而非内置的意义：不需要的人完全不用承担这部分体积。
