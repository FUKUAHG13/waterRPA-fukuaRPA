# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Three-pane catalog for operating cautions and one-time notices."""

import unicodedata

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
)

from ..task_model import config_bool
from ..usage_notices import (
    FIRST_RUN_CHOICE_DONE_KEY,
    USAGE_NOTICES,
    first_use_notices_enabled,
    set_first_use_notices_enabled,
)
from .components import enable_work_window_controls

_GBK_PINYIN_INITIALS = (
    (-20319, "A"),
    (-20284, "B"),
    (-19776, "C"),
    (-19219, "D"),
    (-18711, "E"),
    (-18527, "F"),
    (-18240, "G"),
    (-17923, "H"),
    (-17418, "J"),
    (-16475, "K"),
    (-16213, "L"),
    (-15641, "M"),
    (-15166, "N"),
    (-14923, "O"),
    (-14915, "P"),
    (-14631, "Q"),
    (-14150, "R"),
    (-14091, "S"),
    (-13319, "T"),
    (-12839, "W"),
    (-12557, "X"),
    (-11848, "Y"),
    (-11056, "Z"),
)


def _pinyin_initial(character):
    if unicodedata.category(character)[:1] in {"P", "S", "Z"}:
        return ""
    if character.isascii():
        return character.upper() if character.isalnum() else ""
    try:
        encoded = character.encode("gbk")
    except UnicodeEncodeError:
        return f"~{ord(character):06X}"
    if len(encoded) != 2:
        return f"~{ord(character):06X}"
    code = encoded[0] * 256 + encoded[1] - 65536
    for boundary, initial in reversed(_GBK_PINYIN_INITIALS):
        if code >= boundary:
            return initial
    return f"~{ord(character):06X}"


def usage_name_sort_key(value):
    """Return a deterministic Chinese-name ordering without a runtime dependency.

    Each GBK Han character is mapped to its pinyin initial. The full string key
    compares the second and later initials when titles share the same prefix.
    Unknown Unicode characters keep a deterministic code-point fallback.
    """

    text = str(value or "").strip()
    key = "".join(_pinyin_initial(character) for character in text)
    return key or "ZZ"


def sorted_usage_categories(notices=USAGE_NOTICES):
    categories = []
    seen = set()
    for notice in notices:
        if notice.category in seen:
            continue
        seen.add(notice.category)
        categories.append(notice.category)
    return sorted(categories, key=usage_name_sort_key)


def sorted_usage_notices(category, notices=USAGE_NOTICES):
    return sorted(
        (notice for notice in notices if notice.category == category),
        key=lambda notice: usage_name_sort_key(notice.title),
    )


class UsageNoticesDialog(QDialog):
    """Browse cautions by category and title without closing between selections."""

    def __init__(self, parent, settings):
        super().__init__(parent)
        enable_work_window_controls(self)
        self.settings = settings
        self.setWindowTitle("使用注意")
        self.resize(920, 600)
        self.setMinimumSize(720, 460)
        layout = QVBoxLayout(self)

        heading = QLabel("使用注意与兼容边界")
        heading.setProperty("role", "sectionTitle")
        layout.addWidget(heading)

        self.splitter = QSplitter(Qt.Horizontal)
        self.category_list = QListWidget()
        self.category_list.setMinimumWidth(140)
        self.notice_list = QListWidget()
        self.notice_list.setMinimumWidth(220)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.splitter.addWidget(self.category_list)
        self.splitter.addWidget(self.notice_list)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.splitterMoved.connect(self._persist_splitter)
        layout.addWidget(self.splitter, 1)

        for category in sorted_usage_categories():
            item = QListWidgetItem(category)
            item.setData(Qt.UserRole, category)
            self.category_list.addItem(item)

        self.category_list.currentItemChanged.connect(
            self._show_category
        )
        self.notice_list.currentItemChanged.connect(self._show_notice)
        if self.category_list.count():
            self.category_list.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _show_category(self, current, _previous=None):
        self.notice_list.clear()
        self.details.clear()
        if current is None:
            return
        category = str(current.data(Qt.UserRole) or "")
        for notice in sorted_usage_notices(category):
            item = QListWidgetItem(notice.title)
            item.setData(Qt.UserRole, notice.key)
            self.notice_list.addItem(item)
        if self.notice_list.count():
            self.notice_list.setCurrentRow(0)

    def _show_notice(self, current, _previous=None):
        if current is None:
            self.details.clear()
            return
        key = str(current.data(Qt.UserRole) or "")
        notice = next((entry for entry in USAGE_NOTICES if entry.key == key), None)
        if notice is None:
            self.details.clear()
            return
        lines = [notice.title, "", notice.body]
        if notice.one_time_key:
            shown = config_bool(self.settings.value(notice.one_time_key, False))
            if not first_use_notices_enabled(self.settings):
                state = "自动提示已关闭（可在设置中重新开启）"
            elif shown:
                state = "当前已经提示过。"
            else:
                state = "当前尚未提示。"
            lines.extend(
                [
                    "",
                    "此项也会在相关功能首次使用时提示。" + state,
                ]
            )
        self.details.setPlainText("\n".join(lines))


    def showEvent(self, event):
        super().showEvent(event)
        saved = self.settings.value("usage_notices_splitter", None)
        if isinstance(saved, (QByteArray, bytes, bytearray)):
            self.splitter.restoreState(QByteArray(saved))
        else:
            width = max(600, self.splitter.width())
            self.splitter.setSizes(
                [
                    max(120, int(width * 0.15)),
                    max(200, int(width * 0.32)),
                    max(200, int(width * 0.53)),
                ]
            )

    def _persist_splitter(self, *_args):
        self.settings.setValue("usage_notices_splitter", self.splitter.saveState())

def show_usage_notices(parent, settings):
    return UsageNoticesDialog(parent, settings).exec()


class FirstRunChoiceDialog(QDialog):
    """First-launch choice: keep or turn off automatic first-use notices.

    Two explicit buttons avoid the double negative of a yes/no question. It is
    shown only by the interactive startup path; ``show_first_run_choice`` records
    that the choice is finished either way so the next launch does not interrupt
    the user again.
    """

    RESULT_CLOSE_NOTICES = 1
    RESULT_KEEP_NOTICES = 2

    def __init__(self, parent):
        super().__init__(parent)
        enable_work_window_controls(self)
        self.setWindowTitle("首次使用提示")
        layout = QVBoxLayout(self)

        heading = QLabel("是否保留首次使用提示？")
        heading.setProperty("role", "sectionTitle")
        layout.addWidget(heading)

        body = QLabel(
            "首次使用某些功能时，软件会弹出一次说明（例如首次运行学习急停方式、"
            "选择透明图片、连点器使用中键、切换到详细日志）。\n\n"
            "选择“关闭提示”会同时关闭这些首次使用说明和运行前的风险确认；"
            "错误提示、删除或覆盖确认、完整迁移包的隐私确认不会受影响，仍会正常出现。\n\n"
            "之后可在 设置 → 高级模式 → 界面与提示 中重新开启“首次使用提示”，"
            "也可以在设置中随时用“使用注意”手动查阅全部说明。"
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton(
            "关闭提示", QDialogButtonBox.AcceptRole
        )
        keep_button = buttons.addButton("保留提示", QDialogButtonBox.RejectRole)
        close_button.clicked.connect(
            lambda: self.done(self.RESULT_CLOSE_NOTICES)
        )
        keep_button.clicked.connect(lambda: self.done(self.RESULT_KEEP_NOTICES))
        # 直接关闭窗口（X / Esc）按“保留提示”处理，同时记录选择已结束。
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _sync_widgets_after_first_run_choice(parent, settings):
    """把首次启动选择同步到主窗口上已构建的设置控件。

    设置控件在主窗口构造时就按旧值勾选；若只写 QSettings 不同步控件，
    设置页显示就会与持久状态不一致——用户打开设置看到的仍是已被关闭的
    旧开关状态（反馈 20261010-1）。信号需屏蔽：值已由本函数持久化，
    避免 toggled 再触发一次重复写入与日志。
    """

    if settings is None:
        return
    desired = {
        "risk_tip_chk": config_bool(settings.value("risk_tip_enabled", True)),
        "first_use_notices_chk": first_use_notices_enabled(settings),
    }
    for name, checked in desired.items():
        widget = getattr(parent, name, None)
        if widget is None or widget.isChecked() == checked:
            continue
        widget.blockSignals(True)
        try:
            widget.setChecked(checked)
        finally:
            widget.blockSignals(False)


def show_first_run_choice(parent, settings):
    """Run the first-launch choice and persist its outcome.

    Returns True when the user chose to turn the automatic notices off. Closing
    the window keeps the notices on (the safer default) but still records that the
    choice is finished, so the user is not interrupted again on the next launch.
    """

    dialog = FirstRunChoiceDialog(parent)
    try:
        outcome = dialog.exec()
    finally:
        dialog.deleteLater()
    if outcome == FirstRunChoiceDialog.RESULT_CLOSE_NOTICES:
        set_first_use_notices_enabled(settings, False)
        if settings is not None:
            settings.setValue("risk_tip_enabled", False)
    if settings is not None:
        settings.setValue(FIRST_RUN_CHOICE_DONE_KEY, True)
        settings.sync()
    _sync_widgets_after_first_run_choice(parent, settings)
    return outcome == FirstRunChoiceDialog.RESULT_CLOSE_NOTICES
