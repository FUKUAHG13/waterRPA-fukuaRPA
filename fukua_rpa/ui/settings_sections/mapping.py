"""Key-mapping section and support links for global settings."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...constants import (
    AUTHOR_BILIBILI_URL,
    DEFAULT_KEY_MAPPING_COUNT,
    PROJECT_RELEASES_URL,
    SUPPORTED_WINDOWS_TEXT,
)
from ..components import HelpBtn, ResponsiveRow


def build_mapping_controls(host):
    """构建按键映射区（控件由宿主态创建，此处只入列）。

    迁移（2026-10-19）：本函数**不挂载**到任何布局；返回的容器由调用方收揽并显式
    重定父对象（宿主持有 Python 引用不等于持有 Qt 父对象所有权）。

    2026-10-21：映射已是「工具 → 按键映射」独立窗口，不再需要折叠条；
    改为普通容器，去掉「点一下展开/收起」的标题栏。
    """
    self = host
    g_map = QWidget()
    g_map.setObjectName("mappingToolContent")
    map_main = QVBoxLayout(g_map)
    map_main.setContentsMargins(0, 0, 0, 0)
    # UI-CONVENTION: allow UI004 reason=映射区默认纵向取窄间距 4px：独立窗口里控件块之间
    # 用大间距会显得松散（行内间距仍由 ResponsiveRow 的 7px 控制）。
    map_main.setSpacing(4)
    map_options_row = ResponsiveRow()
    # C-1：mapping_mode_chk / mapping_click_mode_combo / key_mapping_rows /
    # mapping_rows_layout 由宿主态创建（启动链读取），此处只入列。
    map_options_row.add_group(
        self.mapping_mode_chk,
        HelpBtn(
            "【按键映射模式】\n用于把 A、1、Space 这类任意单键映射成鼠标点击。\n开启后，已启用的裸键映射会在全局生效：即使软件在后台，按下该键也会替你点击并拦截这次按键。\n因此裸键只建议在专门执行映射时开启；普通启动/停止快捷键仍建议使用 Ctrl/Alt/Shift 组合或 F 键。"
        ),
    )

    map_options_row.add_group(
        "点击方式:",
        self.mapping_click_mode_combo,
        HelpBtn(
            "【映射点击方式】\n真实鼠标点击：兼容性最好，会把鼠标移动到目标点。\n点击后返回原位：先移动到目标点点击，再立刻回到触发前的鼠标位置；实际鼠标仍会瞬间移动。\n后台窗口点击(实验)：不移动鼠标，只向槽位绑定的目标窗口/控件发送点击消息。先用“取点”确定实际点击坐标，再点“选择窗口”，按提示亲自单击一次目标程序窗口中的任意位置。选窗功能只在本模式可用。目标失效时不会回退真实鼠标点击，以免误点前台窗口。\n只对部分普通窗口/控件有效；游戏、浏览器画布、DirectX 或权限更高的窗口可能无效。若权限不同，请尝试以管理员身份运行本软件。"
        ),
    )
    map_main.addWidget(map_options_row)
    mapping_tools_row = QHBoxLayout()
    add_mapping_btn = QPushButton("+ 添加映射")
    add_mapping_btn.setProperty("variant", "tonal")
    add_mapping_btn.clicked.connect(lambda: self.add_key_mapping_row(refresh=True))
    mapping_tools_row.addWidget(add_mapping_btn)
    mapping_tools_row.addStretch()
    map_main.addLayout(mapping_tools_row)
    # 2026-10-21：映射行放入滚动区。此前直接 addLayout，行数一多窗口被撑高、
    # 必须全部显示；现在给一个可滚动容器，行数超出时出现滚动条。
    rows_container = QWidget()
    self.mapping_rows_layout.setContentsMargins(0, 0, 0, 0)
    rows_container.setLayout(self.mapping_rows_layout)
    self.mapping_rows_scroll = QScrollArea()
    self.mapping_rows_scroll.setWidgetResizable(True)
    self.mapping_rows_scroll.setFrameShape(QFrame.NoFrame)
    self.mapping_rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    self.mapping_rows_scroll.setWidget(rows_container)
    map_main.addWidget(self.mapping_rows_scroll, 1)
    # C-1：启动期 `apply_key_mappings_config` 已建行；仅在为空时补默认行（幂等，
    # 避免「启动建行 + 对话框再建」造成行数翻倍）。
    if not self.key_mapping_rows:
        for _ in range(DEFAULT_KEY_MAPPING_COUNT):
            self.add_key_mapping_row(refresh=False)
    self.refresh_mapping_binding_controls()
    # FR-SCOPE009-002: 映射热键注册失败的界面可见通道。此前失败只进
    # mapping_registration_failures 与日志文件，而 write_log 未传 callback 时并不
    # 进界面日志，用户看不到任何提示，只觉得"热键没反应"。该标签按失败字典的真实
    # 内容显示，无失败项时保持隐藏。
    self.mapping_hotkey_failure_notice = QLabel()
    self.mapping_hotkey_failure_notice.setWordWrap(True)
    self.mapping_hotkey_failure_notice.setProperty("role", "warning")
    self.mapping_hotkey_failure_notice.setVisible(False)
    map_main.addWidget(self.mapping_hotkey_failure_notice)
    # 说明文字只在窗口顶部（key_mapping_tool）出现一次，这里不再重复。
    return g_map


def build_support_links(host, settings_content_layout):
    """构建支持链接行（平台说明、作者主页、下载页）。

    迁移（2026-10-19）：这三项**不属于**按键映射编辑区，必须留在设置页，
    不得随映射区一起搬走或删除。
    """
    self = host
    links_row = QHBoxLayout()
    platform_label = QLabel("支持 Windows 10 / 11 x64")
    platform_label.setProperty("role", "muted")
    platform_label.setToolTip(
        f"系统支持：{SUPPORTED_WINDOWS_TEXT}\n当前版本按 64 位 Python、Qt 和原生 DLL 构建。"
    )
    links_row.addWidget(platform_label)
    links_row.addStretch()
    bilibili_btn = QPushButton("作者B站主页")
    bilibili_btn.setProperty("variant", "ghost")
    bilibili_btn.clicked.connect(lambda: self.open_web_url(AUTHOR_BILIBILI_URL))
    links_row.addWidget(bilibili_btn)
    github_btn = QPushButton("GitHub下载页")
    github_btn.setProperty("variant", "ghost")
    github_btn.clicked.connect(lambda: self.open_web_url(PROJECT_RELEASES_URL))
    links_row.addWidget(github_btn)
    settings_content_layout.addLayout(links_row)

