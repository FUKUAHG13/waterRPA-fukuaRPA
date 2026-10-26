# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Task-type help text and control visibility for an editable task row."""

from ..multi_image_actions import multi_image_action_summary
from ..task_model import until_condition_summary

TASK_TYPE_TIPS = {
    "左键单击": (
        "【左键单击】\n识别目标图片并点击其中心，或直接点击指定屏幕坐标。\n参数格式：图片完整路径 或 屏幕坐标，如：100,100",
        "输入图片路径 或 屏幕坐标，如：960,540",
    ),
    "左键双击": (
        "【左键双击】\n识别目标图片并双击其中心，或直接双击指定屏幕坐标。\n参数格式：图片完整路径 或 屏幕坐标，如：100,100",
        "输入图片路径 或 屏幕坐标，如：960,540",
    ),
    "右键单击": (
        "【右键单击】\n识别目标图片并右击其中心，或直接右击指定屏幕坐标。\n参数格式：图片完整路径 或 屏幕坐标，如：100,100",
        "输入图片路径 或 屏幕坐标，如：960,540",
    ),
    "输入文本": (
        "【输入文本】\n模拟键盘自动输入文本内容（支持中文）。\n参数格式：任意想要输入的文字内容",
        "输入想要发送的文本内容，如：Hello",
    ),
    "等待(秒)": (
        "【等待(秒)】\n强行让脚本暂停执行一段时间，受倍速设置影响。\n参数格式：纯数字，如：1.5 或 3",
        "输入等待的秒数，如：1.5",
    ),
    "滚轮滑动": (
        "【滚轮滑动】\n模拟鼠标滚轮上下滚动。\n参数格式：纯数字（正数向上滚，负数向下滚），如：500 或 -500",
        "输入滚动距离，如：500 或 -500",
    ),
    "系统按键": (
        "【系统按键】\n模拟敲击键盘单键或组合快捷键。\n参数格式：单键(如 A, enter, esc) 或 组合键(如 ctrl+c, alt+tab)",
        "输入按键或组合，如：A、enter 或 ctrl+v",
    ),
    "鼠标悬停": (
        "【鼠标悬停】\n将鼠标移动到指定图片或坐标上方，不进行点击。\n参数格式：图片完整路径 或 屏幕坐标，如：100,100",
        "输入图片路径 或 屏幕坐标，如：960,540",
    ),
    "截图保存": (
        "【截图保存】\n保存整个屏幕、全局识别区域或本步骤单独框选的区域；可在小齿轮里选择。\n参数格式：保存的文件夹目录 或 具体的.png文件路径",
        "输入保存目录，如：D:\\Screenshots",
    ),
    "左键拖拽": (
        "【左键拖拽】\n按住鼠标左键，从起点拖动到终点。\n参数格式：起点 -> 终点。例如：100,100 -> 500,500",
        "输入轨迹坐标，如：100,100 -> 500,500",
    ),
    "右键拖拽": (
        "【右键拖拽】\n按住鼠标右键，从起点拖动到终点。\n参数格式：起点 -> 终点。例如：100,100 -> 500,500",
        "输入轨迹坐标，如：100,100 -> 500,500",
    ),
    "弹窗提醒": (
        "【弹窗提醒】\n暂停脚本并弹出一个系统强制定顶提示框，点击确定后继续。\n参数格式：你想提示的文字内容",
        "输入你想提示的文字，如：任务已完成",
    ),
    "停止运行": (
        "【停止运行】\n执行到此步时，直接强行停止整个脚本的运行。\n参数格式：停止时的日志备注（可选）",
        "输入停止备注，如：条件满足，中止运行",
    ),
    "声音提示": (
        "【声音提示】\n播放系统提示音，并在日志中醒目显示备注，不打断操作。\n参数格式：任意内容（作为日志醒目备注）",
        "输入大号日志备注，如：发现目标！",
    ),
    "直到条件成立": (
        "【直到条件成立】\n判断图片出现/消失、区域变化或区域是否变成指定图片。\n条件未满足时跳回指定步骤，满足后继续下一步或跳到指定步骤。",
        "点小齿轮设置条件；这里会显示条件摘要",
    ),
    "设置变量": (
        "【设置变量】\n为本次运行保存一个变量，重新启动脚本后自动清空。\n格式：变量名 = 表达式，例如 count = count + 1。首次赋值可写 count = 1。",
        "例如：count = 1 或 count = count + 1",
    ),
    "判断表达式": (
        "【判断表达式】\n判断变量和运行状态；结果为真时走成功分支，为假时走失败分支。\n例如：count >= 10 and loop < 3。禁止函数、属性和下标。",
        "例如：count >= 10 and last_success",
    ),
    "启动程序": (
        "【启动程序】\n启动一个程序或执行一条不经过命令解释器的启动命令。\n可点击右侧“程”选择可执行文件。",
        "输入程序路径或启动命令",
    ),
    "等待窗口": (
        "【等待窗口】\n等待标题包含指定文字的窗口出现；以等号开头可要求完整标题相同。\n超时时间优先使用全局超时，未设置时为 10 秒。",
        "输入窗口标题，或点击“窗”选取",
    ),
    "激活窗口": (
        "【激活窗口】\n查找并前置标题匹配的窗口。以等号开头表示完整匹配。",
        "输入窗口标题，或点击“窗”选取",
    ),
    "关闭窗口": (
        "【关闭窗口】\n向标题匹配的窗口发送正常关闭请求，不会强制结束进程。",
        "输入窗口标题，或点击“窗”选取",
    ),
    "输入秘密文本": (
        "【输入秘密文本】\n输入凭据库中指定名称对应的秘密内容；方案只保存名称，不保存秘密本身。",
        "输入凭据名称",
    ),
    "点击窗口控件": (
        "【点击窗口控件】\n通过 Windows UI Automation 激活所选按钮或控件，通常不会移动鼠标。",
        "点击右侧“控”选取目标控件",
    ),
    "设置控件文本": (
        "【设置控件文本】\n通过 Windows UI Automation 把文本写入所选输入控件。",
        "输入要写入控件的文本",
    ),
    "读取控件文本": (
        "【读取控件文本】\n读取所选控件的文本并保存为本次运行变量。",
        "输入变量名，例如 result_text",
    ),
    "多图动作映射": (
        "【多图动作映射】\n按小齿轮中规则的先后顺序识别多张图片，只执行第一条命中规则绑定的动作。",
        "点小齿轮添加图片与对应动作",
    ),
}


_GENERATED_SUMMARY_TYPES = frozenset({"直到条件成立", "多图动作映射"})


def apply_task_type_presentation(row, text):
    unsupported_label = getattr(row, "_unsupported_task_label", "")
    is_unsupported = bool(unsupported_label and text == unsupported_label)
    if unsupported_label and not is_unsupported and not row._loading_data:
        row._unsupported_task_type = None
        row._unsupported_task_label = ""
        row._unsupported_task_payload = None

    previous_type = row._last_type_name
    row._last_type_name = text
    if previous_type in _GENERATED_SUMMARY_TYPES and text != previous_type and not row._loading_data:
        # DESIGN: 恢复进入摘要类型前的原始草稿，而非清空。
        # FR-20260812-D12-001: 草稿键使用可持久化的 saved_value_draft（非 _
        # 前缀），否则保存→重载后草稿丢失，切离摘要类型会把原始值清空；
        # _value_draft 仅作会话内旧键回退。
        draft = row.custom_data.pop("saved_value_draft", "") or row.custom_data.pop(
            "_value_draft", ""
        )
        row.value_input.setText(draft)

    is_until_condition = text == "直到条件成立"
    is_multi_image_action = text == "多图动作映射"
    generated_summary = is_until_condition or is_multi_image_action
    row.value_input.setReadOnly(generated_summary or is_unsupported)
    row.value_input.setProperty("generatedSummary", generated_summary)
    row.value_input.setProperty("unsupportedAction", is_unsupported)
    row.type_combo.setProperty("unsupportedAction", is_unsupported)
    row.value_input.style().unpolish(row.value_input)
    row.value_input.style().polish(row.value_input)
    row.type_combo.style().unpolish(row.type_combo)
    row.type_combo.style().polish(row.type_combo)
    if is_until_condition:
        show_until_condition_summary(row)
    elif is_multi_image_action:
        show_multi_image_action_summary(row)

    if is_unsupported:
        tooltip = (
            "此方案包含当前版本无法识别的动作类型。该步骤会保留原始内容，"
            "但不会获准运行；请选择一个受支持的动作进行主动替换。"
        )
        row.type_combo.setToolTip(tooltip)
        row.value_input.setToolTip(tooltip)
        row.value_input.setPlaceholderText("当前版本不支持此动作，原始参数已保留")
    elif text in TASK_TYPE_TIPS:
        tooltip, placeholder = TASK_TYPE_TIPS[text]
        row.type_combo.setToolTip(tooltip)
        row.value_input.setToolTip(tooltip)
        row.value_input.setPlaceholderText(placeholder)

    if is_unsupported:
        row.file_btn.setVisible(False)
    elif text == "启动程序":
        row.file_btn.setVisible(True)
        row.file_btn.setText("程")
        row.file_btn.setToolTip("选择要启动的程序")
    elif "截图" in text:
        row.file_btn.setVisible(True)
        row.file_btn.setText("夹")
        row.file_btn.setToolTip("选择保存截图的文件夹目录")
    elif "单击" in text or "双击" in text or "悬停" in text:
        row.file_btn.setVisible(True)
        row.file_btn.setText("图")
        row.file_btn.setToolTip(
            "选择本地图片\n性能建议：尽量截取小而独特的目标图片，少包含背景，可降低CPU匹配压力并减少误识别。\n"
            "安全提示：不要只截纯色或几乎没有纹理的区域，这类图片会被程序拒绝。"
        )
    else:
        row.file_btn.setVisible(False)

    row.template_capture_btn.setVisible(
        text in ("左键单击", "左键双击", "右键单击", "鼠标悬停")
    )
    row.sync_data()


def show_until_condition_summary(row):
    # DESIGN: 保存原始值草稿，切离摘要类型时恢复，避免坐标/路径永久丢失。
    # FR-20260812-D12-001: 使用可持久化键 saved_value_draft（非 _ 前缀），
    # 使"保存→重载→切离"链路仍能恢复原始草稿。
    current_value = row.value_input.text()
    if not current_value.startswith("请点击右侧小齿轮修改"):
        row.custom_data["saved_value_draft"] = current_value
    summary = until_condition_summary(row.custom_data)
    row.value_input.setText(f"请点击右侧小齿轮修改 | {summary}")


def show_multi_image_action_summary(row):
    # DESIGN: 保存原始值草稿，切离摘要类型时恢复，避免图片路径永久丢失。
    # FR-20260812-D12-001: 使用可持久化键 saved_value_draft。
    current_value = row.value_input.text()
    if not current_value.startswith("请点击右侧小齿轮修改"):
        row.custom_data["saved_value_draft"] = current_value
    summary = multi_image_action_summary(row.custom_data.get("multi_image_actions", []))
    row.value_input.setText(f"请点击右侧小齿轮修改 | {summary}")
