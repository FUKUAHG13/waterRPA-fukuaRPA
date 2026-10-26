# 办公文档插件（office-docs）

fukuaRPA 的**选装插件**之一，补齐「Excel / PDF 业务语义」这一当前主线明确不支持
的能力缺口。基础程序保持轻量，**默认不带 openpyxl / pypdf**，需要的人再装本插件。

> 本插件与「本地 OCR 插件」「浏览器自动化插件」同属第二层选装插件，均遵循
> `fukuaRPA.plugin` 插件契约。

## 信任边界

fukuaRPA 核心承诺**永远不联网**。本插件：

- **不声明任何网络权限**（v1 权限枚举也无 `network` 项）。
- 所有文档读写均为**本地文件操作**（用户显式指定的路径），数据不出本机。
- 体积大头是 `openpyxl`（仅 ~2MB，很小）与 `pypdf`，按需在用户机器上
  `pip install`，不进 fukuaRPA 主程序。

## 提供的动作

### Excel（.xlsx）

| 动作 id | 作用 | 关键参数 |
|---|---|---|
| `excel_read_cell` | 读单元格 | `path`, `sheet`, `cell` |
| `excel_read_range` | 读区域 | `path`, `sheet`, `start`, `end` |
| `excel_write_cell` | 写单元格 | `path`, `sheet`, `cell`, `value` |
| `excel_write_range` | 批量写区域 | `path`, `sheet`, `start`, `values` |
| `excel_find` | 查找值 | `path`, `sheet`, `value`, `mode`(contains/equals) |
| `excel_sheets` | 列出工作表 | `path` |
| `excel_row_count` | 行数 | `path`, `sheet` |

> 注：openpyxl 仅支持 `.xlsx`，不支持旧版 `.xls`。

### PDF

| 动作 id | 作用 | 关键参数 |
|---|---|---|
| `pdf_page_count` | 页数 | `path` |
| `pdf_extract_text` | 提取文本 | `path`, `pages`(如 `1-3,5`) |
| `pdf_search` | 搜索文本 | `path`, `query`, `mode`(contains/regex) |
| `pdf_merge` | 合并 | `inputs`, `output` |
| `pdf_split` | 拆分 | `path`, `ranges`, `output_dir` |

## 目录结构

```
office-docs/
├── manifest.json              # 插件清单（由 build_plugin.py 生成）
├── src/office_docs/           # 运行时源码
│   ├── action.py              # 宿主入口 run() / execute_action()
│   ├── engine.py              # 动作编排
│   ├── excel_backend.py       # Openpyxl 后端 + Mock 后端
│   └── pdf_backend.py         # Pypdf 后端 + Mock 后端
├── tools/
│   ├── build_plugin.py        # 生成清单 + 打包 + 原项目契约校验
│   └── office_cli.py          # 独立命令行工具（现在就能用）
├── tests/test_office_docs.py
├── requirements.txt
├── LICENSE
└── README.md
```

## 使用方式

### 1. 独立模式（现在就能用）

```bash
pip install -r requirements.txt
python tools/office_cli.py --action excel_read_cell \
    --payload '{"path":"a.xlsx","sheet":"Sheet1","cell":"A1"}'
python tools/office_cli.py --action pdf_search \
    --payload '{"path":"b.pdf","query":"自动化"}' --backend mock
```

### 2. fukuaRPA 插件模式

待宿主 P3 运行时落地后，用 `tools/build_plugin.py` 生成 `office-docs.fkrplugin`，
在 fukuaRPA 中安装即可在步骤里选用上述动作。宿主按 `manifest.json` 的
`entrypoint`（`src/office_docs/action.py:run`）加载并执行。

## 验证

```bash
python tests/test_office_docs.py     # 单元测试（Mock 后端，无需 openpyxl/pypdf）
python tools/build_plugin.py --validate     # 用原项目契约校验 manifest
```
