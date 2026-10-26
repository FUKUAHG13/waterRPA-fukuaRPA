"""Recognition and scale-memory sections for global settings."""

from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QPushButton, QVBoxLayout

from ...constants import NATIVE_CORE_DLL_NAME
from ..components import (
    CollapsibleSection,
    HelpBtn,
    NoWheelComboBox,
    NoWheelSpinBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)


def build_recognition_sections(host, settings_content_layout):
    """构建识别与缩放内存设置区。

    `self = host` 仅为沿用原主窗口构建代码的书写习惯，控件实际挂载到主窗口 host。
    """
    self = host
    # ================= 核心折叠设置区 =================
    # 1. 识别配置
    g1 = CollapsibleSection("全局识别配置")
    gl1_wrap = QVBoxLayout()
    recognition_settings_row = ResponsiveRow()
    self.conf_edit = QLineEdit("0.8")
    self.conf_edit.setFixedWidth(70)
    recognition_settings_row.add_group(
        "相似度:",
        self.conf_edit,
        HelpBtn(
            "【相似度 (0.1 - 1.0)】\n数值越低：越容易匹配，但极易导致乱点误触。\n数值越高：越精确。"
        ),
    )
    self.gray_chk = QCheckBox("灰度匹配")
    self.gray_chk.setChecked(True)
    recognition_settings_row.add_group(
        self.gray_chk,
        HelpBtn(
            "【灰度匹配】\n开启后极快且省CPU。如果两张图形状一样颜色不同，请关闭！"
        ),
    )
    self.scale_min = QLineEdit("0.8")
    self.scale_min.setFixedWidth(70)
    self.scale_max = QLineEdit("1.2")
    self.scale_max.setFixedWidth(70)
    recognition_settings_row.add_group(
        "缩放范围:",
        self.scale_min,
        "至",
        self.scale_max,
        HelpBtn(
            "【缩放范围】\n程序启动时会预先生成缩放模板缓存。建议不要超过 0.8 - 2.0。\n为防止内存耗尽，单张图片最多生成 61 个缩放档位，缩放值最高为 5.0。"
        ),
    )
    self.scale_step = QLineEdit("0.05")
    self.scale_step.setFixedWidth(70)
    scale_step_group = recognition_settings_row.add_group(
        "步长:",
        self.scale_step,
        HelpBtn("【缩放步长】\n默认值 0.05，调低会增加CPU压力。"),
    )
    self.scale_step_group = scale_step_group
    self.advanced_setting_widgets.append(scale_step_group)
    self.native_core_chk = QCheckBox("启用DLL原生识别")
    self.native_core_chk.setChecked(True)
    self.native_core_chk.setToolTip(
        f"开启后优先使用内置 {NATIVE_CORE_DLL_NAME} 做图片识别；"
        "不可用或不适合时自动回退到 OpenCV/Python 识别。"
    )
    native_core_group = recognition_settings_row.add_group(
        self.native_core_chk,
        HelpBtn(
            "【DLL原生识别】\n开启后，图片点击/悬停会优先尝试使用内置 C++ DLL 识别核心，通常可降低部分识别场景的 CPU 压力并提高速度。\n如果遇到兼容性问题、识别结果异常，或想对比旧版效果，可以关闭；关闭后完全使用原来的 OpenCV/Python 识别路径。\nDLL 加载失败时会自动回退，不会影响脚本启动。"
        ),
    )
    self.advanced_setting_widgets.append(native_core_group)
    self.native_parallel_combo = NoWheelComboBox()
    self.native_parallel_combo.addItem("自动（推荐）", "auto")
    self.native_parallel_combo.addItem("关闭（单线程）", "off")
    self.native_parallel_combo.addItem("强制多核", "force")
    fit_combo_to_contents(self.native_parallel_combo)
    native_parallel_width = self.native_parallel_combo.sizeHint().width()
    self.native_parallel_combo.setMinimumWidth(native_parallel_width)
    self.native_parallel_combo.setMaximumWidth(native_parallel_width)
    native_parallel_group = recognition_settings_row.add_group(
        "原生多核:",
        self.native_parallel_combo,
        HelpBtn(
            "【原生多核】\n仅在启用 DLL 原生识别时生效。\n自动（推荐）：根据缩放档位、识别区域和计算量动态使用最多 8 个线程，避免 CPU 长时间满载。\n关闭（单线程）：最省调度开销，适合很小的识别区域或排查兼容问题。\n强制多核：始终尽量使用多核，可能加快大范围、多缩放识别，也可能提高瞬时 CPU 占用。"
        ),
    )
    self.advanced_setting_widgets.append(native_parallel_group)
    self.native_scale_hint_chk = QCheckBox("启用缩放记忆")
    self.native_scale_hint_chk.setChecked(True)
    native_scale_hint_group = recognition_settings_row.add_group(
        self.native_scale_hint_chk,
        HelpBtn(
            "【缩放记忆】\n仅对“快速一个”模式的搜索顺序生效。程序会统计同一图片在本次软件启动期间成功命中的倍率，并优先检查常用倍率；优先倍率未命中时仍会在同一截图上搜索完整范围。\n“全部匹配”始终搜索全部缩放档位。关闭软件后自动学习记录会清空。"
        ),
    )
    self.advanced_setting_widgets.append(native_scale_hint_group)
    gl1_wrap.addWidget(recognition_settings_row)
    g1.set_content_layout(gl1_wrap)
    settings_content_layout.addWidget(g1)

    self.scale_memory_section = CollapsibleSection(
        "缩放记忆", expanded=False
    )
    g_scale_memory = self.scale_memory_section
    scale_memory_rows = QVBoxLayout()
    scale_memory_row = ResponsiveRow()
    self.scale_memory_tier_combo = NoWheelComboBox()
    self.scale_memory_tier_combo.addItem("稳妥", "conservative")
    self.scale_memory_tier_combo.addItem("均衡（推荐）", "balanced")
    self.scale_memory_tier_combo.addItem("积极", "aggressive")
    fit_combo_to_contents(self.scale_memory_tier_combo)
    scale_memory_row.add_group(
        "自动策略:",
        self.scale_memory_tier_combo,
        HelpBtn(
            "【自动策略】\n三个档位不会对应固定数量，而是影响算法选择历史容量和优先倍率数量的倾向。\n稳妥：更少的优先倍率、更短的历史；均衡：兼顾速度与适应变化；积极：保留更多历史并覆盖更多常见倍率。"
        ),
    )
    self.scale_memory_manual_edit = QLineEdit()
    self.scale_memory_manual_edit.setPlaceholderText("例如 0.8, 1.0, 1.2")
    self.scale_memory_manual_edit.setMinimumWidth(180)
    scale_memory_row.add_group(
        "手动优先倍率:",
        self.scale_memory_manual_edit,
        HelpBtn(
            "【手动优先倍率】\n可填写多个倍率，用逗号或空格分隔，最多 12 个。手动倍率会排在自动学习结果之前；不在当前步骤缩放范围/步长中的值会自动忽略。\n图片点击的详细或完全日志会显示本次命中的准确缩放倍率，可直接参考日志填写。"
        ),
    )
    scale_memory_rows.addWidget(scale_memory_row)

    custom_memory_row = ResponsiveRow()
    self.scale_memory_custom_chk = QCheckBox("自定义记忆容量")
    custom_memory_row.add_group(
        self.scale_memory_custom_chk,
        HelpBtn(
            "【自定义记忆容量】\n默认关闭，由算法根据缩放档位数量、已观察倍率和所选策略动态决定。\n只有熟悉脚本行为时再开启；开启后才能手动修改下面两项。"
        ),
    )
    self.scale_memory_preferred_spin = NoWheelSpinBox()
    self.scale_memory_preferred_spin.setRange(1, 12)
    self.scale_memory_preferred_spin.setValue(3)
    self.scale_memory_preferred_spin.setSuffix(" 个")
    custom_memory_row.add_group(
        "学习优先倍率上限:",
        self.scale_memory_preferred_spin,
        HelpBtn("只限制算法从历史中选出的倍率数量，不包含手动优先倍率。"),
    )
    self.scale_memory_history_spin = NoWheelSpinBox()
    self.scale_memory_history_spin.setRange(8, 512)
    self.scale_memory_history_spin.setValue(64)
    self.scale_memory_history_spin.setSuffix(" 次")
    custom_memory_row.add_group(
        "历史记录上限:",
        self.scale_memory_history_spin,
        HelpBtn(
            "最多保留多少次成功命中记录。记录只保存在本次软件启动期间，不写入磁盘。"
        ),
    )
    self.scale_memory_clear_btn = QPushButton("清除本次记忆")
    fit_text_button(self.scale_memory_clear_btn, 104)
    self.scale_memory_clear_btn.setProperty("variant", "ghost")
    self.scale_memory_clear_btn.setToolTip(
        "清空本次软件启动期间自动学习到的缩放倍率；手动设置不会被清除。"
    )
    custom_memory_row.add_group(self.scale_memory_clear_btn)
    scale_memory_rows.addWidget(custom_memory_row)

    self.scale_memory_status_label = QLabel(
        "本次启动尚未学习到缩放倍率；首次成功识别后会显示当前优先倍率和算法历史上限。"
    )
    self.scale_memory_status_label.setWordWrap(True)
    self.scale_memory_status_label.setProperty("role", "muted")
    scale_memory_rows.addWidget(self.scale_memory_status_label)
    g_scale_memory.set_content_layout(scale_memory_rows)
    settings_content_layout.addWidget(g_scale_memory)
    self.advanced_setting_widgets.append(g_scale_memory)
