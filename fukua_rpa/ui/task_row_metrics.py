# pyright: reportAttributeAccessIssue=false
"""正常步骤行的共享布局度量（滚动预览与实际行共用的唯一依据）。

抽取自基线提交 ``1ba6f11`` 的 ``task_row_view.py`` / ``presentation.py``，
常量取值与原始代码逐项一致；``tests/test_task_row_metrics_contract.py``
以基线 golden 值锁定，防止抽取时抄错造成两套几何漂移。

本模块只做纯几何计算，不创建 QWidget、不读方案数据，便于委托在 paint
热路径复用并在离屏测试中独立验证。
"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontMetrics

# --- 基线 golden 常量（来源：基线 task_row_view.py / presentation.py）---
# 正文控件行 QHBoxLayout contentsMargins(8, 5, 7, 5) / spacing(6)
CONTROLS_MARGIN_LEFT = 8
CONTROLS_MARGIN_TOP = 5
CONTROLS_MARGIN_RIGHT = 7
CONTROLS_MARGIN_BOTTOM = 5
CONTROLS_SPACING = 6

# 分组头 QHBoxLayout contentsMargins(8 + depth*14, 3, 8, 3) / spacing(6)
HEADER_MARGIN_LEFT_BASE = 8
HEADER_MARGIN_DEPTH_STEP = 14
HEADER_MARGIN_TOP = 3
HEADER_MARGIN_RIGHT = 8
HEADER_MARGIN_BOTTOM = 3
HEADER_SPACING = 6
HEADER_TOGGLE_SIZE = 24

# 标记列固定 20x20；动作列 minWidth 130；按钮列 30；编号列 BASE_WIDTH 32 + 12 padding
MARK_SIZE = 20
TYPE_MIN_WIDTH = 130
ACTION_BUTTON_WIDTH = 30
INDEX_BASE_WIDTH = 32
INDEX_PADDING = 12

# presentation 控件缩进：layout.setContentsMargins(8 + depth*10, 5, 7, 5)
CONTROL_DEPTH_STEP = 10

# 正文可见列（正常行自左向右）：编号 + 标记(修/停/断) + 动作 + 参数 + 右侧操作区。
# 右侧操作区的按钮数量取基线行视图的可见集合，用于计算留白宽度。
RIGHT_ACTION_BUTTONS = 6  # 图/框/测/取/齿轮/删除（"展开录制"按数据条件显示，此处不计）

# 参数列最少保留的可读宽度（低于此值时优先压缩标记显示，见 delegate）
VALUE_MIN_WIDTH = 40


def header_indent(depth):
    """分组头左侧缩进：depth 为 1-based，与 presentation.build_group_presentations 一致。"""
    depth = max(1, min(3, int(depth)))
    return HEADER_MARGIN_LEFT_BASE + (depth - 1) * HEADER_MARGIN_DEPTH_STEP


def control_indent(depth):
    """正文控件行左侧缩进：depth 为 0-based，与 set_group_presentation 一致。"""
    depth = max(0, min(3, int(depth)))
    return CONTROLS_MARGIN_LEFT + depth * CONTROL_DEPTH_STEP


def index_width(metrics, maximum_index):
    """编号列宽：与 StepIndexLabel.sizeHint() 同款算法。

    metrics 为 QFontMetrics；maximum_index 用于按最大序号预留宽度。
    """
    if not isinstance(metrics, QFontMetrics):
        metrics = QFontMetrics(metrics)
    text_width = metrics.horizontalAdvance(f"{max(1, int(maximum_index))}.")
    return max(INDEX_BASE_WIDTH, text_width + INDEX_PADDING)


def visible_mark_width(mark_count):
    """可见标记列总宽（每个 MARK_SIZE，列间 spacing）。"""
    count = max(0, int(mark_count))
    if count == 0:
        return 0
    return count * MARK_SIZE + (count - 1) * CONTROLS_SPACING


def right_action_width(button_count=RIGHT_ACTION_BUTTONS):
    """右侧操作区列宽（按钮 + 列间 spacing）。"""
    count = max(0, int(button_count))
    if count == 0:
        return 0
    return count * ACTION_BUTTON_WIDTH + (count - 1) * CONTROLS_SPACING


def content_columns(rect, *, depth, maximum_index, metrics,
                    mark_count=0, right_buttons=RIGHT_ACTION_BUTTONS,
                    value_min=VALUE_MIN_WIDTH):
    """计算正文行各列矩形，返回 dict：index/marks/type/value/actions。

    DESIGN：与真实 QHBoxLayout 的列顺序、固定宽度、最小宽度和 spacing 同源；
    参数列取剩余空间，窄时守住 value_min，动作列守住 TYPE_MIN_WIDTH。
    """
    if not isinstance(metrics, QFontMetrics):
        metrics = QFontMetrics(metrics)
    left = rect.left() + control_indent(depth)
    top = rect.top() + CONTROLS_MARGIN_TOP
    bottom = rect.bottom() - CONTROLS_MARGIN_BOTTOM
    height = max(1, bottom - top)
    spacing = CONTROLS_SPACING

    idx_w = index_width(metrics, maximum_index)
    marks_w = visible_mark_width(mark_count)
    actions_w = right_action_width(right_buttons)
    fixed = idx_w + marks_w + actions_w + 3 * spacing  # 4 段之间的 3 个间距
    # 动作列与参数列之间还有 1 个 spacing，剩余空间按 动作 min + 参数 min 分配
    available = rect.width() - (left - rect.left()) - CONTROLS_MARGIN_RIGHT - fixed - spacing
    available = max(0, available)

    type_w = TYPE_MIN_WIDTH
    value_w = available - type_w
    if value_w < value_min:
        # 窄窗：先压缩动作列到其仍可读的最小（至少 TYPE_MIN_WIDTH 的一半），再压参数列
        type_min = max(60, TYPE_MIN_WIDTH // 2)
        shrink = min(TYPE_MIN_WIDTH - type_min, value_min - value_w)
        type_w -= max(0, shrink)
        value_w = max(0, available - type_w)

    x = left
    columns = {}
    columns["index"] = QRect(x, top, idx_w, height)
    x += idx_w + spacing
    if marks_w:
        columns["marks"] = QRect(x, top, marks_w, height)
        x += marks_w + spacing
    else:
        columns["marks"] = QRect(x, top, 0, height)
    columns["type"] = QRect(x, top, type_w, height)
    x += type_w + spacing
    columns["value"] = QRect(x, top, value_w, height)
    x += value_w + spacing
    columns["actions"] = QRect(x, top, actions_w, height)
    return columns


def header_layout(rect, depth, title_width=0, count_width=0):
    """分组头内部矩形：toggle / title / count，与真实 QFrame 同序。"""
    depth = max(1, min(3, int(depth)))
    left = rect.left() + header_indent(depth)
    top = rect.top() + HEADER_MARGIN_TOP
    bottom = rect.bottom() - HEADER_MARGIN_BOTTOM
    height = max(1, bottom - top)
    toggle = QRect(left, top, HEADER_TOGGLE_SIZE, HEADER_TOGGLE_SIZE)
    title_x = left + HEADER_TOGGLE_SIZE + HEADER_SPACING
    title = QRect(title_x, top, max(0, title_width), height)
    count_x = rect.right() - HEADER_MARGIN_RIGHT - max(0, count_width)
    count = QRect(count_x, top, max(0, count_width), height)
    return {"toggle": toggle, "title": title, "count": count}


def elide(metrics, text, width):
    """按列宽截断文本；宽度不足时返回空串，避免溢出相邻列。"""
    if width <= 0 or not text:
        return ""
    if not isinstance(metrics, QFontMetrics):
        metrics = QFontMetrics(metrics)
    return metrics.elidedText(str(text), Qt.ElideRight, int(width))
