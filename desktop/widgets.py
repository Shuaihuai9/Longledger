"""小型自定义控件：数据概览卡、字号分段切换。"""
from __future__ import annotations

from typing import Callable, Optional

from desktop.qt import (
    QBrush,
    QColor,
    QComboBox,
    QCompleter,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QIcon,
    QLabel,
    QLineEdit,
    QPainter,
    QPen,
    QPointF,
    QPolygonF,
    QSizePolicy,
    QStringListModel,
    QTableView,
    QTextCharFormat,
    Qt,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from desktop.theme import COLORS, TONE_COLORS


class MetricCard(QFrame):
    """顶部/分析页的数据卡：小标签 + 大数字 + 一行注释。"""

    def __init__(self, title: str, compact: bool = False) -> None:
        super().__init__()
        self.setObjectName("metricCardCompact" if compact else "metricCard")
        self.setMinimumHeight(84 if compact else 102)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(3)

        self._title = QLabel(title)
        self._title.setObjectName("metricLabel")
        self._value = QLabel("—")
        self._value.setObjectName("metricValueCompact" if compact else "metricValue")
        self._hint = QLabel("")
        self._hint.setObjectName("metricHint")

        layout.addWidget(self._title)
        layout.addWidget(self._value)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def set_value(self, text: str, tone: Optional[str] = None) -> None:
        self._value.setText(text)
        color = TONE_COLORS.get(tone or "ink", COLORS["ink"])
        self._value.setStyleSheet(f"color: {color}; background: transparent;")

    def set_hint(self, text: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))


class HistoryLineEdit(QLineEdit):
    """证券代码输入框：点击即列出历史代码，↑↓ 选择、回车填充。"""

    def __init__(self, history: Callable[[], list[str]], parent=None) -> None:
        super().__init__(parent)
        self._history = history
        self._completer = QCompleter(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion
        )
        self.setCompleter(self._completer)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        # 点击事件先交给 QLineEdit 处理（定位光标），弹出延后到事件循环
        QTimer.singleShot(0, self._show_history)

    def _show_history(self) -> None:
        popup = self._completer.popup()
        if popup is not None and popup.isVisible():
            return
        codes = list(self._history() or [])
        self._completer.setModel(QStringListModel(codes, self._completer))
        if not codes:
            return
        self._completer.complete()


class PaperTableView(QTableView):
    """统一表格：所有列都能拖拽调宽，首次可见时按内容给一次默认宽度。

    Qt 的表格在不显示时算出的列宽只包含表头，所以定宽必须等控件真正可见；
    之后宽度交给用户，刷新数据不会再覆盖手动调整。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._columns_fitted = False
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)

    def setModel(self, model) -> None:  # noqa: N802 - Qt 命名
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self.fit_columns_once)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if not self._columns_fitted:
            QTimer.singleShot(0, self.fit_columns_once)

    def reset_column_widths(self) -> None:
        """字号变化后重新按内容定宽；不可见时推迟到下次显示。"""
        self._columns_fitted = False
        if self.isVisible():
            self.fit_columns_once()

    def fit_columns_once(self) -> None:
        model = self.model()
        if self._columns_fitted or model is None or model.rowCount() == 0:
            return
        self._columns_fitted = True
        self.resizeColumnsToContents()
        for column in range(model.columnCount()):
            if self.columnWidth(column) > 320:
                self.setColumnWidth(column, 320)


def _draw_chevron(widget: QWidget) -> None:
    """在控件右侧 22px 下拉区居中画一条古铜折线（与 QSS 下拉色块对齐）。"""
    color = QColor(COLORS["accent"] if widget.isEnabled() else COLORS["muted"])
    painter = QPainter(widget)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center_x = widget.width() - 12.0
        center_y = widget.height() / 2.0
        half_width = 4.0
        half_height = 2.0
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(center_x - half_width, center_y - half_height),
                    QPointF(center_x, center_y + half_height),
                    QPointF(center_x + half_width, center_y - half_height),
                ]
            )
        )
    finally:
        painter.end()


class PaperDateEdit(QDateEdit):
    """纸面日期框：右侧下拉区底色由 QSS 绘制，折角箭头在这里补画。

    QSS 一旦给 ::drop-down 设了背景，Qt 就不再画原生箭头，所以手绘一条细折线，
    与 PaperStyle 中的数字框箭头保持一致。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 为「yyyy-MM-dd + 左右内边距 + 右侧 22px 下拉区」留足宽度，避免末位被截断
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("8888-88-88") + 46)

    def setCalendarPopup(self, enabled: bool) -> None:  # noqa: N802 - Qt 接口
        super().setCalendarPopup(enabled)
        if enabled:
            self._tint_calendar()

    def _tint_calendar(self) -> None:
        """把日历弹层拉回纸面：周末用低饱和暗红，翻月按钮换成细折角。"""
        calendar = self.calendarWidget()
        if calendar is None:
            return
        weekend = QTextCharFormat()
        weekend.setForeground(QBrush(QColor(COLORS["up"])))
        calendar.setWeekdayTextFormat(Qt.DayOfWeek.Saturday, weekend)
        calendar.setWeekdayTextFormat(Qt.DayOfWeek.Sunday, weekend)
        for name, text in (("qt_calendar_prevmonth", "‹"), ("qt_calendar_nextmonth", "›")):
            button = calendar.findChild(QToolButton, name)
            if button is not None:
                button.setIcon(QIcon())
                button.setText(text)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().paintEvent(event)
        if self.calendarPopup():
            _draw_chevron(self)


class PaperComboBox(QComboBox):
    """纸面下拉框：右侧下拉区同样由 QSS 改色，箭头在这里补画。"""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().paintEvent(event)
        _draw_chevron(self)


class YearTailDateEdit(PaperDateEdit):
    """Tab 进入时自动选中年度后两位，直接键入两位年份即可。"""

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        if event.reason() == Qt.FocusReason.TabFocusReason:
            edit = self.lineEdit()
            if edit is not None:
                edit.setSelection(2, 2)


# 表格控件见上方 PaperTableView