"""纸质账簿风配色与 QSS。

色调取向：浅米黄羊皮纸、深灰褐墨色、低饱和暗红（盈利）/ 墨绿（亏损）、
古铜色作为强调色。所有控件共用同一套样式，避免局部「现代控件」破坏阅读感。
"""
from __future__ import annotations

from string import Template

from desktop.qt import (
    QColor,
    QPainter,
    QPen,
    QPointF,
    QPolygonF,
    QProxyStyle,
    QStyle,
    Qt,
)

COLORS = {
    "bg": "#EDE5D3",            # 主背景：浅米黄羊皮纸
    "panel": "#FAF5E8",         # 卡片/表格底：更亮的纸面
    "panel_alt": "#F4EDDD",     # 斑马纹
    "sidebar": "#E5DAC2",       # 左侧导航：略深的纸脊
    "sidebar_hover": "#EEE4CE",
    "line": "#D3C5A6",          # 主分割线
    "line_soft": "#E6DCC6",     # 次级分割线
    "ink": "#3A332A",           # 正文：深灰褐，不用纯黑
    "ink_soft": "#605746",
    "muted": "#6E6450",         # 次要文字：比正文浅一档，但保证在纸面上可读
    "accent": "#8A6A33",        # 古铜色
    "accent_soft": "#F0E3C4",
    "up": "#9C463C",            # 盈利：低饱和暗红
    "down": "#4F7350",          # 亏损：墨绿
    "input": "#FFFCF4",
}

TONE_COLORS = {
    "up": COLORS["up"],
    "down": COLORS["down"],
    "muted": COLORS["muted"],
    "accent": COLORS["accent"],
    "ink": COLORS["ink"],
}

# 表格正文 / 表头字号（像素）；原「大」档，现已固定为唯一档位
TABLE_FONT_PX = 17


class PaperStyle(QProxyStyle):
    """把原生三角箭头换成手绘细线折角，统一日期框 / 数字框 / 下拉框的观感。"""

    _ARROW_DIRECTIONS = {
        QStyle.PrimitiveElement.PE_IndicatorArrowDown: -1,
        QStyle.PrimitiveElement.PE_IndicatorSpinDown: -1,
        QStyle.PrimitiveElement.PE_IndicatorArrowUp: 1,
        QStyle.PrimitiveElement.PE_IndicatorSpinUp: 1,
    }

    def __init__(self, base_style: str = "Fusion") -> None:
        super().__init__(base_style)

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802 - Qt 接口
        direction = self._ARROW_DIRECTIONS.get(element)
        if direction is None:
            super().drawPrimitive(element, option, painter, widget)
            return

        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        color = QColor(COLORS["accent"] if enabled else COLORS["muted"])
        rect = option.rect
        center_x = rect.center().x()
        center_y = rect.center().y()
        half_width = max(2.5, min(4.0, rect.width() * 0.22))
        half_height = max(1.5, min(2.5, rect.height() * 0.16))

        if direction < 0:
            points = [
                QPointF(center_x - half_width, center_y - half_height),
                QPointF(center_x, center_y + half_height),
                QPointF(center_x + half_width, center_y - half_height),
            ]
        else:
            points = [
                QPointF(center_x - half_width, center_y + half_height),
                QPointF(center_x, center_y - half_height),
                QPointF(center_x + half_width, center_y + half_height),
            ]

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF(points))
        painter.restore()


_QSS = Template(
    """
* { outline: none; }

QWidget {
    color: $ink;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}

QMainWindow, QWidget#Root { background: $bg; }

/* ---------------- 二级窗口 / 右键菜单 ---------------- */
QDialog, QMessageBox {
    background: $bg;
    color: $ink;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMessageBox { min-width: 360px; }
QMessageBox QLabel { color: $ink; font-size: 13px; padding: 3px 2px; }

QMenu {
    background: $panel;
    border: 1px solid $line;
    border-radius: 4px;
    padding: 5px 4px;
    color: $ink;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMenu::item {
    padding: 7px 22px 7px 14px;
    border-radius: 3px;
}
QMenu::item:selected { background: $accent_soft; color: $ink; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $line_soft; margin: 4px 10px; }

/* ---------------- 左侧导航 ---------------- */
QFrame#sidebar {
    background: $sidebar;
    border: none;
    border-right: 1px solid $line;
}
QLabel#brandTitle {
    font-family: "Songti SC", "SimSun", "Georgia", serif;
    font-size: 30px;
    font-weight: bold;
    color: $ink;
}
QLabel#brandSub { font-size: 10px; color: $muted; }
QLabel#sectionLabel { font-size: 11px; color: $muted; }
QLabel#sidebarFoot { font-size: 10px; color: $muted; }
QLabel#sidebarVersion { font-size: 10px; color: $muted; }

QFrame#rule { background: $line_soft; border: none; }

QPushButton#navButton {
    text-align: left;
    padding: 11px 18px;
    border: none;
    border-left: 3px solid transparent;
    background: transparent;
    color: $ink_soft;
    font-size: 14px;
}
QPushButton#navButton:hover { background: $sidebar_hover; color: $ink; }
QPushButton#navButton:checked {
    background: $accent_soft;
    border-left: 3px solid $accent;
    color: $ink;
    font-weight: bold;
}

/* ---------------- 顶部 ---------------- */
QLabel#pageTitle {
    font-family: "Songti SC", "SimSun", "Georgia", serif;
    font-size: 24px;
    font-weight: bold;
    color: $ink;
}
QLabel#pageSubtitle { font-size: 12px; color: $muted; }

QPushButton#ghostButton {
    background: transparent;
    border: 1px solid $line;
    border-radius: 4px;
    padding: 5px 16px;
    color: $ink_soft;
    font-size: 12px;
}
QPushButton#ghostButton:hover { border-color: $accent; color: $accent; }
QPushButton#ghostButton:pressed { background: $accent_soft; }

/* ---------------- 数据概览卡 ---------------- */
QFrame#metricCard, QFrame#metricCardCompact {
    background: $panel;
    border: 1px solid $line_soft;
    border-radius: 6px;
}
QLabel#metricLabel { font-size: 11px; color: $muted; }
/* 数字统一用现代衬线体：Cambria / Times 的「等高数字」不会出现 3/4/7/9 高低错落 */
QLabel#metricValue {
    font-family: "Cambria", "Times New Roman", "Songti SC", serif;
    font-size: 26px;
    font-weight: bold;
    color: $ink;
}
QLabel#metricValueCompact {
    font-family: "Cambria", "Times New Roman", "Songti SC", serif;
    font-size: 20px;
    font-weight: bold;
    color: $ink;
}
QLabel#metricHint { font-size: 10px; color: $muted; }

/* ---------------- 卡片与表格 ---------------- */
QFrame#card {
    background: $panel;
    border: 1px solid $line_soft;
    border-radius: 6px;
}
QLabel#cardTitle { font-size: 15px; font-weight: bold; color: $ink; }
QLabel#cardHint { font-size: 11px; color: $muted; }
QLabel#emptyHint { font-size: 12px; color: $muted; }
QLabel#formStatus { font-size: 12px; }

QTableView {
    background: $panel;
    alternate-background-color: $panel_alt;
    border: none;
    selection-background-color: $accent_soft;
    selection-color: $ink;
    gridline-color: transparent;
}
QTableView::item {
    padding: 2px 10px;
    border: none;
    border-bottom: 1px solid $line_soft;
}
QTableView::item:selected { background: $accent_soft; color: $ink; }

QHeaderView { background: transparent; }
QHeaderView::section {
    background: $panel;
    color: $muted;
    padding: 9px 10px;
    border: none;
    border-bottom: 1px solid $line;
    font-size: 12px;
    font-weight: bold;
}

/* ---------------- 表单控件 ---------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {
    background: $input;
    border: 1px solid $line;
    border-radius: 3px;
    padding: 6px 9px;
    color: $ink;
    selection-background-color: $accent_soft;
    selection-color: $ink;
    min-height: 18px;
}
/* 日期/数字框的内嵌编辑区不再重复描边与内边距，右侧空间留给下拉区与箭头 */
QDateEdit QLineEdit, QSpinBox QLineEdit, QDoubleSpinBox QLineEdit,
QDateEdit QLineEdit:focus, QSpinBox QLineEdit:focus, QDoubleSpinBox QLineEdit:focus,
QDateEdit QLineEdit:disabled, QSpinBox QLineEdit:disabled, QDoubleSpinBox QLineEdit:disabled {
    background: transparent;
    border: none;
    padding: 0;
    min-height: 0;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QComboBox:focus { border: 1px solid $accent; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QDateEdit:disabled, QComboBox:disabled { color: $muted; background: $panel_alt; }

QSpinBox::up-button, QDoubleSpinBox::up-button, QDateEdit::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateEdit::down-button {
    width: 16px;
    border: none;
    background: transparent;
}
/* 日期框右侧下拉区：去掉 Windows 原生按钮，改为纸面小色块 + 手绘折角箭头 */
QDateEdit::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    border-left: 1px solid $line_soft;
    border-top-right-radius: 3px;
    border-bottom-right-radius: 3px;
    background: $panel_alt;
}
QDateEdit::drop-down:hover, QDateEdit::drop-down:on { background: $accent_soft; }

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    border-left: 1px solid $line_soft;
    background: $panel_alt;
}
QComboBox::drop-down:hover { background: $accent_soft; }
QComboBox QAbstractItemView {
    background: $input;
    border: 1px solid $line;
    selection-background-color: $accent_soft;
    selection-color: $ink;
}

/* 日期选择面板（QDateEdit 的日历弹层） */
QCalendarWidget QWidget#qt_calendar_navigationbar { background: $panel_alt; }
QCalendarWidget QToolButton {
    background: transparent;
    border: none;
    border-radius: 3px;
    padding: 5px 9px;
    color: $ink;
    font-weight: bold;
}
QCalendarWidget QToolButton:hover { background: $accent_soft; }
/* 日历内部表格不吃全局 QTableView 的单元格内边距，否则两位数日期会被省略成「…」 */
QCalendarWidget QTableView::item { padding: 0; border: none; }
QCalendarWidget QAbstractItemView:enabled {
    background: $panel;
    color: $ink;
    selection-background-color: $accent_soft;
    selection-color: $ink;
}
QCalendarWidget QSpinBox {
    background: $input;
    border: 1px solid $line;
    border-radius: 3px;
}

QPushButton {
    background: $input;
    border: 1px solid $line;
    border-radius: 4px;
    padding: 6px 16px;
    color: $ink_soft;
    font-size: 12px;
}
QPushButton:hover { border-color: $accent; color: $accent; }
QPushButton:pressed { background: $accent_soft; }
QPushButton:disabled { color: $muted; background: $panel_alt; border-color: $line_soft; }

QPushButton#primaryButton {
    background: $accent;
    color: #FBF6E9;
    border: none;
    border-radius: 4px;
    padding: 9px 26px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton#primaryButton:hover { background: #9A7A42; }
QPushButton#primaryButton:pressed { background: #775E2C; }
QPushButton#primaryButton:disabled { background: $line; color: $muted; }

QLabel#fieldLabel { font-size: 11px; color: $ink_soft; }

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: $line;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: $line;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QToolTip {
    background: $panel;
    color: $ink;
    border: 1px solid $line;
    padding: 5px 8px;
}
"""
)


def build_stylesheet() -> str:
    """生成全局 QSS。表格字号由视图自身统一设置（TABLE_FONT_PX）。"""
    return _QSS.substitute(COLORS)
