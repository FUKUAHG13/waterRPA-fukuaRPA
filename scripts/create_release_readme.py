"""Write the end-user portable release and trust notice."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    SUPPORTED_WINDOWS_TEXT,
)
from scripts.hashutil import atomic_write_text  # noqa: E402


def release_readme_text() -> str:
    return f"""fukuaRPA {APP_VERSION} 完整多文件版
========================================

支持范围
--------
{SUPPORTED_WINDOWS_TEXT}，仅 x64。

使用方法
--------
1. 必须完整解压整个 {BUILD_NAME}_portable.zip。
2. 进入 {BUILD_NAME} 文件夹，双击 {BUILD_NAME}.exe。
3. 不要只复制 EXE；_internal 目录和其他随包文件都是运行所需内容。
4. 配置、导入图片和日志默认保存在程序解压目录，请放在有写入权限的位置。

离线与更新
----------
fukuaRPA 本身始终不联网：在整个运行期间不访问互联网或局域网，不发送遥测，不自动检查更新，
也不进行后台下载。实验性独立 Runner 只在本机进程之间通信，不向外部网络发送数据。设置页中的
作者主页和 GitHub 按钮只在用户主动点击时把公开网址交给系统默认浏览器；网页访问由浏览器完成，
不是 fukuaRPA 在联网。

完整性与签名
------------
设置页的“校验程序”可检查文件是否缺失或损坏。发布目录外以 _SHA256SUMS.txt 结尾的文件可核对便携 ZIP
和逐文件清单，但这些哈希不能单独证明发布者身份。

当前发布没有作者代码签名证书，Windows 显示“未知发布者”属于预期状态。项目不会使用测试证书
伪装正式签名。请只从作者公开的 GitHub Releases 页面获取文件，并对照同时发布的 SHA-256。

发布物料
--------
- LICENSE：项目 MIT 许可证与上游/本项目版权声明
- BUILD_INFO.json：源码指纹、依赖和构建环境
- SBOM.cdx.json：CycloneDX 软件物料清单
- THIRD_PARTY_NOTICES.txt / THIRD_PARTY_LICENSES.txt：第三方声明与许可证
- RELEASE_INFO.json：主程序和关键 DLL 哈希、真实 Authenticode 状态
- RUNTIME_CLOSURE.json：包内 EXE/DLL/PYD 的 x64 架构与依赖闭包审计
- PAYLOAD_HASHES.json：设置页使用的包内完整性清单
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    if not release_dir.is_dir():
        raise SystemExit(f"Release directory not found: {release_dir}")
    output = release_dir / "README_RELEASE.txt"
    atomic_write_text(output, release_readme_text())
    print(output)


if __name__ == "__main__":
    main()
