"""Central, UI-independent catalog of user-facing operating cautions."""

from __future__ import annotations

from dataclasses import dataclass

from .task_model import config_bool

# 首用提示总开关：只控制“自动弹出”，不改变任何 *_seen 的已看过状态，也不影响
# 手动查阅（“使用注意”窗口、帮助按钮）。关闭后重新开启，尚未真正看过的提示仍会
# 在对应首次使用入口显示。见 docs/设计基准.md。
FIRST_USE_NOTICES_ENABLED_KEY = "onboarding/first_use_notices_enabled"
# 首次启动选择是否已经结束（已选择或已关闭该窗口），避免下次启动重复打断。
FIRST_RUN_CHOICE_DONE_KEY = "onboarding/first_run_choice_done"


@dataclass(frozen=True)
class UsageNotice:
    """Immutable catalog entry for a user-facing operating caution.

    Attributes:
        key: Stable lookup key used by UI and tests to reference this notice.
        category: Human-readable category for grouping in settings/help UI.
        title: Short heading displayed in tooltips or notice panels.
        body: Full explanatory text shown to the user.
        one_time_key: Optional persistence key; when non-empty the UI records
            that the user has acknowledged this notice so it is not repeated.
    """

    key: str
    category: str
    title: str
    body: str
    one_time_key: str = ""


USAGE_NOTICES = (
    UsageNotice(
        "fast_response",
        "速度与识图",
        "快速反应场景",
        "用于抢答、打地鼠等场景时，可尝试将“间隔”设为 0，并适当降低“识别间隔”。"
        "如果目标长时间未出现后响应变慢，可关闭“自适应降频”。数值越低，CPU 占用可能越高。",
        "onboarding/fast_response_tip_v1_seen",
    ),
    UsageNotice(
        "template_quality",
        "速度与识图",
        "模板图片应小而独特",
        "尽量只框住目标本身，少包含大面积背景，并避免纯色或几乎没有纹理的区域。"
        "模板越大、缩放档位越多，识别所需的 CPU 和缓存通常越多；过于简单的模板也更容易误识别。",
    ),
    UsageNotice(
        "transparent_template",
        "速度与识图",
        "透明图片会降低识别速度",
        "软件会自动忽略完全透明像素，并按 Alpha 透明度给其余像素加权。"
        "透明模板无法使用当前的原生高速识别路径，通常会慢于普通图片。"
        "如果不需要透明效果，建议使用“快捷选图”直接从屏幕框选目标。",
        "onboarding/transparent_template_warning_v1_seen",
    ),
    UsageNotice(
        "masked_template",
        "速度与识图",
        "忽略局部变化仍需保留足够特征",
        "“忽略图片局部变化”会把框选区域从匹配中排除，并继续按整张模板的位置点击；"
        "目标整体移动后仍可识别。忽略区域过大、剩余内容过于普通或边缘重复时，误识别风险会增加。"
        "此功能会自动使用 OpenCV Alpha 掩膜路径，通常慢于普通不透明模板。",
    ),
    UsageNotice(
        "recognition_regions",
        "速度与识图",
        "识别区域并非越多越快",
        "多区域识别适合目标区域彼此相隔较远、且总面积只占屏幕很小一部分的场景。"
        "区域相邻或覆盖面积很大时，合并为一个矩形通常更简单，也可能更快。",
    ),
    UsageNotice(
        "step_region_precedence",
        "速度与识图",
        "本步骤识别区域会替代全局区域",
        "图片步骤启用“本步骤识别区域”后，该步骤只在自己的矩形区域内查找，不再使用全局单区域或多区域。"
        "直接输入坐标的点击、等待和其他非图片动作不会应用这项设置。",
    ),
    UsageNotice(
        "image_variants",
        "速度与识图",
        "特殊图片匹配会使用较慢路径",
        "透明模板、忽略局部变化和反色模板匹配都需要 OpenCV 完整匹配，不能使用当前原生高速路径。"
        "反色匹配会同时接受明暗相反但结构相近的目标，只有确有这种状态变化时才应开启。",
    ),
    UsageNotice(
        "background_click",
        "操作与兼容",
        "后台点击存在窗口兼容边界",
        "后台点击不会移动真实鼠标，也不会在失败时退回前台点击。"
        "它只对部分普通 Win32 或 UI Automation 控件有效；游戏、浏览器画布、DirectX、"
        "最小化窗口或权限更高的程序可能忽略消息。权限不同时可尝试以管理员身份运行。",
    ),
    UsageNotice(
        "hotkeys",
        "操作与兼容",
        "启动热键与按键映射用途不同",
        "为避免打字时误启动脚本，启动和停止热键不接受裸字母、裸数字、空格或回车。"
        "F1-F12 和带 Ctrl、Alt、Shift、Win 的组合键可用于启停；裸字母或数字可用于按键映射。",
    ),
    UsageNotice(
        "isolated_runner",
        "操作与兼容",
        "独立 Runner 仍属于实验功能",
        "独立 Runner 把脚本执行放到子进程，便于隔离崩溃和强制停止，但会增加启动准备时间，"
        "也可能被安全软件拦截。启动失败会明确提示，不会静默切换到另一执行路径。",
    ),
    UsageNotice(
        "emergency_stop",
        "安全与执行",
        "运行前确认急停方式",
        "建议至少保留一种急停方式，并先用无风险脚本确认启停热键可用。"
        "软件不能控制 UAC 安全桌面、锁屏界面、受保护游戏或权限更高且拒绝普通输入的程序。",
    ),
    UsageNotice(
        "script_emergency_stop_methods",
        "安全与执行",
        "首次运行：三种急停方式",
        "运行脚本前请确认至少掌握一种急停方式：\n"
        "1）主界面“停止”按钮，或设置中配置的“停止热键”；\n"
        "2）右上角急停（任务栏托盘）——在设置“急停方式”中开启；\n"
        "3）任务管理器急停 / ESC 或中键急停——同样在设置“急停方式”中开启。\n"
        "建议先用无风险脚本验证启停确实可用，再运行真实任务。",
        "onboarding/script_emergency_stop_methods_v1_seen",
    ),
    UsageNotice(
        "auto_clicker_middle_button",
        "安全与执行",
        "中键连点会占用鼠标中键",
        "连点器使用鼠标中键时，中键本身用于连续点击，设置中的“鼠标中键急停”会在连点器运行期间"
        "暂时不生效，停止连点器后自动恢复，无需手动更改设置。脚本与连点器不会同时运行；"
        "请记住连点器窗口中显示的停止快捷键，必要时也可点击“停止”按钮。",
        "onboarding/auto_clicker_middle_button_v1_seen",
    ),
    UsageNotice(
        "loop_risk",
        "安全与执行",
        "条件循环可能长期运行",
        "“无限重复”“禁止跳过”和“直到条件成立”都可能让步骤长期等待。"
        "软件会在首次运行检测到明显循环风险时提示；正式使用前建议设置超时、检查上限或明确的停止条件。",
    ),
    UsageNotice(
        "timed_stop",
        "安全与执行",
        "指定时间有两种停止方式",
        "默认的“本轮结束后停止”会等当前脚本循环完整结束，因此遇到无限重复、禁止跳过或无上限条件循环时，"
        "可能明显超过设定时间。“尽快停止当前步骤”会在安全取消点结束当前动作。"
        "手动停止和急停始终尽快生效，不受该选项影响。",
    ),
    UsageNotice(
        "simple_advanced_visibility",
        "操作与兼容",
        "简易模式只隐藏高级参数",
        "从高级模式切回简易模式不会清空或关闭已经设置的高级参数，这些参数仍会正常生效。"
        "只有调试断点在简易模式下暂不触发，切回高级模式后恢复。",
    ),
    UsageNotice(
        "editor_organization",
        "操作与兼容",
        "备注、颜色、隐藏和分组只整理界面",
        "步骤备注、底色标记、编辑器隐藏和最多三级的折叠分组都不会改变执行顺序、分支目标或步骤编号。"
        "被隐藏或折叠的步骤仍会照常执行；需要临时不执行某一步时，应使用“暂时禁用本步骤”。",
    ),
    UsageNotice(
        "coordinate_preview_dragging",
        "操作与兼容",
        "坐标总预览按分组整体移动",
        "在主界面的坐标总预览中，可拖动普通直接坐标或自定义点位的圆点来单独修正；"
        "拖动连线会整体移动该连线两端所属的最深共同分组。全部未分组步骤共同视为一个隐式顶层组，"
        "与各一级组平级；移动父组会包含其子组。跨越不同一级组，或跨越未分组步骤与一级组的连线"
        "只用于展示，不能拖动。坐标步进派生点、悬停点和拖拽起终点暂不支持单点修正，但可随所属组整体移动。",
    ),
    UsageNotice(
        "disabled_hidden_entry",
        "安全与执行",
        "隐藏、禁用和仅跳转执行含义不同",
        "“编辑器隐藏”只影响显示；“暂时禁用”会在运行时略过该步骤；“仅被跳转或指定执行时运行”"
        "只阻止正常顺序进入，成功/失败跳转、先执行指定步骤和单步调试仍可执行它。"
        "被略过的步骤不会产生成功或失败结果。",
    ),
    UsageNotice(
        "explicit_step_call",
        "安全与执行",
        "先执行指定步骤会返回调用者",
        "当前步骤完成本次结果后（因禁用等原因直接略过时除外），可先附属执行一个指定步骤，再回到当前步骤进行成功/失败跳转判定。"
        "被指定步骤会执行自己的重复、重试和超时，但其分支与再次指定其他步骤会被忽略，避免形成隐蔽调用链。",
    ),
    UsageNotice(
        "batch_step_settings",
        "操作与兼容",
        "批量修改只应用兼容参数",
        "简易模式只显示全部所选步骤共有的设置；高级模式可以显示全部设置，但保存时仍只把图片参数应用到图片步骤、"
        "坐标参数应用到坐标步骤。未改动的混合值默认保持原样，只有明确勾选统一时才会被覆盖。",
    ),
    UsageNotice(
        "coordinate_sequences",
        "安全与执行",
        "自定义点位拥有自己的进度",
        "自定义点位仅对直接坐标点击生效，并会停用同一步的坐标步进。每次进入本步骤可点击一个、指定数量或全部剩余点；"
        "点完后再按“跳过本步”“停在最后一个”或“循环”的设置处理。停止并重新启动脚本会重置这份运行进度。",
    ),
    UsageNotice(
        "auxiliary_clicks",
        "安全与执行",
        "两种补点都不会阻塞主流程",
        "“识别确认补点”会在延时后确认同一图片仍在原位置附近才补点；“延时二次点击”不重新识别，直接在原点击位置补点。"
        "两者都是附属动作，不参与步骤重复、循环或分支判定；停止脚本会取消尚未执行的补点。",
    ),
    UsageNotice(
        "timing_layers",
        "安全与执行",
        "间隔、识别间隔与等待用途不同",
        "“间隔”发生在相邻步骤之间；图片步骤开始识别前还会应用“识别间隔”，两者可能叠加。"
        "显式的“等待”步骤会屏蔽其前后原本的步骤间隔，确保等待时长不被重复计算。",
    ),
    UsageNotice(
        "display_coordinates",
        "操作与兼容",
        "坐标取决于当前显示器布局",
        "屏幕坐标、识别区域和窗口相对点位都与录制时的分辨率、系统缩放和多显示器排列有关。"
        "更换电脑、改变缩放或调整显示器位置后，应先预览点位并测试识别。",
    ),
    UsageNotice(
        "profile_compatibility",
        "数据与隐私",
        "新版本不保证兼容旧方案",
        "软件更新可能调整方案结构，不承诺新旧版本之间可以互相读取。升级或回退前请保留原程序，"
        "并使用普通导出、完整迁移包或方案备份保存可恢复副本。",
    ),
    UsageNotice(
        "branch_precedence",
        "安全与执行",
        "跳至优先于跳过",
        "同一成功或失败结果同时设置“跳至”和“跳过”时，软件优先执行“跳至”。"
        "因暂时禁用、循环范围或本次运行上限而被略过的步骤，不会触发成功或失败分支。",
    ),
    UsageNotice(
        "backup_scope",
        "数据与隐私",
        "自动备份不能代替迁移包",
        "备份数量 0 表示关闭，1 表示只保留最新恢复副本，N 表示最新副本加 N-1 份历史记录。"
        "自动备份不会复制模板图片；跨电脑迁移含图片的可运行方案应使用完整迁移包。",
    ),
    UsageNotice(
        "import_review",
        "安全与执行",
        "运行导入方案前先检查动作",
        "导入方案本身不能执行任意 Python，但可以点击、输入、发送快捷键、启动程序或关闭窗口。"
        "请先查看导入审查摘要、缺失资源和危险动作，再决定是否启用并运行。",
    ),
    UsageNotice(
        "offline_operation",
        "数据与隐私",
        "程序本身始终不联网",
        "fukuaRPA 在整个运行期间不访问互联网或局域网，不发送遥测，不自动检查更新，也不进行后台下载。"
        "实验性独立 Runner 只在本机进程之间通信，不向外部网络发送数据。"
        "只有您主动点击作者主页或 GitHub 按钮时，软件才把公开网址交给系统默认浏览器；"
        "网页访问由浏览器完成，不是 fukuaRPA 在联网。",
    ),
    UsageNotice(
        "portable_export",
        "数据与隐私",
        "完整迁移包包含私人内容",
        "完整迁移包用于把可运行方案迁移到自己的其他电脑，会保留普通脚本文字、变量表达式、"
        "窗口信息、程序路径和模板图片。请只交给可信任的人或设备。"
        "凭据库秘密值和“输入秘密文本”的原文始终不会导出。",
        "portable_export_privacy_notice_seen",
    ),
    UsageNotice(
        "export_types",
        "数据与隐私",
        "三种导出的用途不同",
        "普通导出只生成 JSON，不复制模板图片，适合同一台电脑备份。"
        "完整迁移包生成可迁移 ZIP，保留模板与普通工作流内容。"
        "脱敏分享包会移除文字、路径、窗口绑定和模板图片，并停用受影响步骤，只适合排查结构。",
    ),
    UsageNotice(
        "logging",
        "数据与隐私",
        "详细日志会增加界面刷新开销",
        "详细日志会记录更多流程信息，完全日志还会输出参数、耗时、缓存和回退统计。"
        "排错完成后可切回简易日志或关闭界面日志。日志隐私保护默认开启；关闭后分享日志前应重新开启。",
        "ack_heavy_log_warning",
    ),
    UsageNotice(
        "credentials",
        "数据与隐私",
        "凭据不能随方案迁移",
        "凭据秘密值由 Windows 按当前用户加密，不会进入方案、日志或导出文件。"
        "换电脑或换 Windows 用户后，需要在凭据库中重新建立同名凭据。",
    ),
    UsageNotice(
        "scheduled_task_logs",
        "数据与隐私",
        "计划日志会有界保存在本机",
        "计划任务的运行摘要和详细日志保存在当前软件数据目录，方便关闭计划窗口或重启软件后继续查看。"
        "详细文本受当前日志隐私保护设置约束，达到容量上限后会自动淘汰最早内容；"
        "在计划设置中清除运行历史会同时删除这些日志。",
    ),
    UsageNotice(
        "scheduled_tasks_lock_screen",
        "安全与执行",
        "锁屏或休眠时计划任务不会执行",
        "计划任务依赖本程序在运行。电脑锁屏、休眠、睡眠或关机期间，已到点的任务不会执行；"
        "唤醒或重新开机后，单次/每日等任务会按“错过执行”策略补跑一次或跳过，具体取决于该任务的设置。",
        "onboarding/scheduled_tasks_lock_screen_v1_seen",
    ),
    UsageNotice(
        "scheduled_tasks_missed",
        "安全与执行",
        "程序未运行时计划任务不生效",
        "只有本程序正在运行且未被其他脚本占用时，计划任务才会到点触发。"
        "若到点时正好有手动脚本在运行，可按任务的“冲突处理”选择跳过或仅补跑一次；"
        "程序完全关闭时错过的任务不会在下次启动时自动批量补跑全部历史。",
    ),
    UsageNotice(
        "plugin_permissions",
        "数据与隐私",
        "第三方插件只具有声明过的权限",
        "插件以离线方式运行，不访问互联网，也不读取方案与凭据。"
        "每个插件在安装时会声明其需要的权限（如读取屏幕、读写文件），启用前请确认来源可信。"
        "插件只能执行其清单中声明的动作；未知插件或权限不符的动作会在运行前被拦截。",
        "onboarding/plugin_permissions_v1_seen",
    ),
)

NOTICE_BY_KEY = {notice.key: notice for notice in USAGE_NOTICES}


def usage_notice(key: str) -> UsageNotice:
    """Retrieve a usage notice by its stable key.

    Args:
        key: The unique notice identifier (e.g. ``"fast_response"``).

    Returns:
        The matching UsageNotice instance.

    Raises:
        KeyError: If no notice with the given key exists in the catalog.
    """
    return NOTICE_BY_KEY[str(key)]


def first_use_notices_enabled(settings) -> bool:
    """Return whether automatic first-use notices are enabled (default on)."""

    if settings is None:
        return True
    return config_bool(settings.value(FIRST_USE_NOTICES_ENABLED_KEY, True))


def set_first_use_notices_enabled(settings, enabled: bool) -> None:
    """Persist the master switch without touching any ``*_seen`` marker."""

    if settings is None:
        return
    settings.setValue(FIRST_USE_NOTICES_ENABLED_KEY, bool(enabled))
    settings.sync()


def automatic_first_use_notice_pending(settings, one_time_key: str) -> bool:
    """Whether *one_time_key* may be shown automatically right now.

    The caller must still record the ``*_seen`` key only when it really shows the
    notice; a suppressed notice must leave the marker untouched so re-enabling
    the master switch can still present it at its next first-use entry point.
    """

    if not one_time_key or settings is None:
        return False
    if not first_use_notices_enabled(settings):
        return False
    return not config_bool(settings.value(one_time_key, False))
