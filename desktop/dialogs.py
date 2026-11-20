"""桌面端弹窗：修改成交、单标的明细、新建账户，以及中文按钮的确认框。

弹窗只收集输入并回调主窗口方法，账务计算仍全部落在 services / core。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from config.settings import (
    LEDGER_MODE_LABELS,
    LEDGER_MODE_STATEMENT,
    LEDGER_MODE_SYSTEM,
    LEDGER_MODES,
)
from core.fees import SIDE_SELL
from desktop.models import RecordRow, RecordsModel, trade_record_row
from desktop.qt import (
    QCheckBox,
    QDate,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    Qt,
    QVBoxLayout,
)
from desktop.widgets import PaperComboBox, PaperDateEdit


def confirm(parent, title: str, text: str, ok_text: str = "确定") -> bool:
    """中文按钮的确认框；默认按钮是「确定」，回车直接确认，Esc 仍取消。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    # 不用系统自带的问号图标，保持纸面留白
    box.setIcon(QMessageBox.Icon.NoIcon)
    # 先建按钮再挂进 QMessageBox：objectName 在 polish 之前设置，才能套上主按钮样式
    ok = QPushButton(ok_text)
    ok.setObjectName("primaryButton")
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel = QPushButton("取消")
    cancel.setObjectName("ghostButton")
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    box.addButton(ok, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(ok)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is ok


def warn(parent, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.NoIcon)
    button = QPushButton("知道了")
    button.setObjectName("primaryButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    box.addButton(button, QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(button)
    box.setEscapeButton(button)
    box.exec()


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _dialog_buttons(dialog: QDialog, ok_text: str) -> tuple[QHBoxLayout, QPushButton]:
    row = QHBoxLayout()
    row.setSpacing(12)
    row.addStretch(1)
    cancel = QPushButton("取消")
    cancel.setObjectName("ghostButton")
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel.clicked.connect(dialog.reject)
    ok = QPushButton(ok_text)
    ok.setObjectName("primaryButton")
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    # 回车必须落在确认按钮上：取消按钮不参与默认键竞争（Esc 仍可取消）
    ok.setDefault(True)
    ok.setAutoDefault(True)
    cancel.setAutoDefault(False)
    cancel.setDefault(False)
    row.addWidget(cancel)
    row.addWidget(ok)
    return row, ok


def _price(value) -> str:
    return "—" if value is None else f"{value:.3f}"


class TradeEditDialog(QDialog):
    """修改一笔成交的代码 / 日期 / 数量 / 成交价。

    方向不可改：买改卖属于另一笔业务，删除重记比原地改向更不容易出错。
    """

    def __init__(self, trade, parent=None) -> None:
        super().__init__(parent)
        side = "卖出" if trade.side == SIDE_SELL else "买入"
        self._is_sell = trade.side == SIDE_SELL
        self.setWindowTitle(f"修改成交 · {trade.symbol} {trade.trade_date.isoformat()}")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        head = QLabel(f"方向：{side}（方向不可改，如需改方向请删除后重新记一笔）")
        head.setObjectName("cardHint")
        head.setWordWrap(True)
        layout.addWidget(head)

        self.symbol_edit = QLineEdit(trade.symbol)
        self.symbol_edit.setMaxLength(12)

        self.date_edit = PaperDateEdit(
            QDate(trade.trade_date.year, trade.trade_date.month, trade.trade_date.day)
        )
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)

        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 100_000_000)
        self.qty_spin.setSingleStep(100)
        self.qty_spin.setGroupSeparatorShown(True)
        self.qty_spin.setValue(trade.qty)

        self.price_spin = QDoubleSpinBox()
        self.price_spin.setDecimals(3)
        self.price_spin.setRange(0.001, 100_000.0)
        self.price_spin.setSingleStep(0.01)
        self.price_spin.setValue(trade.price_mills / 1000)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        specs = (
            ("证券代码", self.symbol_edit),
            ("成交日期", self.date_edit),
            ("数量 (股)", self.qty_spin),
            ("成交价 (元)", self.price_spin),
        )
        for column, (text, widget) in enumerate(specs):
            grid.addWidget(_field_label(text), 0, column)
            grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

        # 决策记录（可选）：与成交一起保存，之后在标的明细里回看
        decision_title = QLabel("决策记录（可选）")
        decision_title.setObjectName("cardTitle")
        layout.addWidget(decision_title)

        self.logic_edit = QPlainTextEdit()
        self.logic_edit.setPlaceholderText("决策逻辑：为什么这样操作，依据什么信号或估值")
        self.logic_edit.setPlainText(getattr(trade, "trade_logic", ""))
        self.logic_edit.setFixedHeight(66)
        layout.addWidget(self.logic_edit)

        self.plan_combo = PaperComboBox()
        self.plan_combo.addItems(TradePlanDialog.PLAN_OPTIONS)
        self.plan_combo.setCurrentText(getattr(trade, "plan_type", "") or "不填")

        cycle_label = "是否改变买入逻辑" if side == "卖出" else "预期持有周期"
        self.cycle_combo = PaperComboBox()
        if side == "卖出":
            self.cycle_combo.addItems(TradePlanDialog.YES_NO_OPTIONS)
            self.cycle_combo.setCurrentText(getattr(trade, "strategy_changed", "") or "不填")
        else:
            self.cycle_combo.addItems(TradePlanDialog.HOLD_OPTIONS)
            self.cycle_combo.setCurrentText(getattr(trade, "hold_period", "") or "不填")

        decision_grid = QGridLayout()
        decision_grid.setHorizontalSpacing(14)
        decision_grid.setVerticalSpacing(5)
        for column, (text, widget) in enumerate(
            (("计划内 / 计划外", self.plan_combo), (cycle_label, self.cycle_combo))
        ):
            decision_grid.addWidget(_field_label(text), 0, column)
            decision_grid.addWidget(widget, 1, column)
            decision_grid.setColumnStretch(column, 1)
        layout.addLayout(decision_grid)

        note = QLabel("保存后将按剩余流水重新计算批次、现金、红利税与公司行为。")
        note.setObjectName("cardHint")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons, ok = _dialog_buttons(self, "保存修改")
        ok.clicked.connect(self._accept)
        layout.addLayout(buttons)

    def _accept(self) -> None:
        if not self.symbol_edit.text().strip():
            warn(self, "修改成交", "请先填写证券代码。")
            return
        self.accept()

    def values(self) -> tuple[str, date, int, str]:
        qd = self.date_edit.date()
        return (
            self.symbol_edit.text().strip(),
            date(qd.year(), qd.month(), qd.day()),
            self.qty_spin.value(),
            f"{self.price_spin.value():.3f}",
        )

    def notes(self) -> dict:
        """修改成交时一并保存的决策记录；下拉选「不填」即写空删除旧值。"""
        def pick(combo) -> str:
            text = combo.currentText()
            return "" if text == "不填" else text

        notes = {
            "trade_logic": self.logic_edit.toPlainText().strip(),
            "plan_type": pick(self.plan_combo),
        }
        if self._is_sell:
            notes["strategy_changed"] = pick(self.cycle_combo)
        else:
            notes["hold_period"] = pick(self.cycle_combo)
        return notes


class TradePlanDialog(QDialog):
    """成交前的可选交易决策表单。

    买入：决策逻辑 / 计划内计划外 / 预期持有周期；
    卖出：决策逻辑 / 计划内计划外 / 是否改变买入逻辑。
    全部可以留空，直接点「确定入账」与原来的确认流程等价；
    填写的内容只随成交保存，不参与任何金额计算。
    """

    PLAN_OPTIONS = ("不填", "计划内", "计划外")
    HOLD_OPTIONS = ("不填", "短线", "中期", "长期")
    YES_NO_OPTIONS = ("不填", "否", "是")

    def __init__(self, side: str, symbol: str, parent=None) -> None:
        super().__init__(parent)
        label = "卖出" if side == SIDE_SELL else "买入"
        self._is_sell = side == SIDE_SELL
        self.setWindowTitle("交易决策（可选）")
        self.setModal(True)
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        head = QLabel(
            f"{label} {symbol} 的决策记录，全部可选：留空直接点「确定入账」即可，"
            "填写后可在标的明细的「决策记录」中查看。"
        )
        head.setObjectName("cardHint")
        head.setWordWrap(True)
        layout.addWidget(head)

        self.logic_edit = QPlainTextEdit()
        self.logic_edit.setPlaceholderText("决策逻辑：为什么买 / 为什么卖，依据什么信号或估值")
        self.logic_edit.setFixedHeight(76)

        self.plan_combo = PaperComboBox()
        self.plan_combo.addItems(self.PLAN_OPTIONS)

        cycle_label = "是否改变买入逻辑" if self._is_sell else "预期持有周期"
        self.cycle_combo = PaperComboBox()
        self.cycle_combo.addItems(
            self.YES_NO_OPTIONS if self._is_sell else self.HOLD_OPTIONS
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        for column, (text, widget) in enumerate(
            (("计划内 / 计划外", self.plan_combo), (cycle_label, self.cycle_combo))
        ):
            grid.addWidget(_field_label(text), 0, column)
            grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 1)

        layout.addWidget(_field_label("决策逻辑"))
        layout.addWidget(self.logic_edit)
        layout.addLayout(grid)

        buttons, ok = _dialog_buttons(self, "确定入账")
        ok.clicked.connect(self.accept)
        layout.addLayout(buttons)

    def values(self) -> dict:
        """返回决策字段；未填写的一律给空值，与不填等价。"""
        def pick(combo) -> str:
            text = combo.currentText()
            return "" if text == "不填" else text

        notes = {
            "trade_logic": self.logic_edit.toPlainText().strip(),
            "plan_type": pick(self.plan_combo),
        }
        if self._is_sell:
            notes["strategy_changed"] = pick(self.cycle_combo)
        else:
            notes["hold_period"] = pick(self.cycle_combo)
        return notes


class ManualDividendDialog(QDialog):
    """补录一条公司行为（ETF 分红 / 送转）。

    自动同步覆盖不到的场景（多为 ETF 数据源缺失）用它兜底；重复事件会按
    除权日 + 每股派息 + 送转比例去重，重复录入不会二次计账。
    """

    def __init__(self, symbol: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"补录派息 · {symbol}")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        head = QLabel(
            f"为 {symbol} 补录一条分红 / 送转。只作用于除权日当天在册的批次，"
            "已自动同步过的事件会自动去重。"
        )
        head.setObjectName("cardHint")
        head.setWordWrap(True)
        layout.addWidget(head)

        self.ex_date = PaperDateEdit()
        self.ex_date.setDate(QDate.currentDate())

        self.cash_spin = QDoubleSpinBox()
        self.cash_spin.setDecimals(3)
        self.cash_spin.setRange(0.0, 10_000.0)
        self.cash_spin.setSingleStep(0.01)
        self.cash_spin.setSpecialValueText("无")

        self.bonus_spin = QDoubleSpinBox()
        self.bonus_spin.setDecimals(3)
        self.bonus_spin.setRange(0.0, 10.0)
        self.bonus_spin.setSingleStep(0.1)
        self.bonus_spin.setSpecialValueText("无")

        self.transfer_spin = QDoubleSpinBox()
        self.transfer_spin.setDecimals(3)
        self.transfer_spin.setRange(0.0, 10.0)
        self.transfer_spin.setSingleStep(0.1)
        self.transfer_spin.setSpecialValueText("无")

        self.prev_spin = QDoubleSpinBox()
        self.prev_spin.setDecimals(3)
        self.prev_spin.setRange(0.0, 100_000.0)
        self.prev_spin.setSingleStep(0.01)
        self.prev_spin.setSpecialValueText("自动获取")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        specs = (
            ("除权日", self.ex_date),
            ("每股派息 (元)", self.cash_spin),
            ("每股送股 (股)", self.bonus_spin),
            ("每股转增 (股)", self.transfer_spin),
        )
        for index, (text, widget) in enumerate(specs):
            row, column = divmod(index, 2)
            grid.addWidget(_field_label(text), row * 2, column)
            grid.addWidget(widget, row * 2 + 1, column)
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

        self.same_day = QCheckBox("派息到账日与除权日相同")
        self.same_day.setChecked(True)
        self.pay_date = PaperDateEdit()
        self.pay_date.setDate(QDate.currentDate())
        self.pay_date.setEnabled(False)
        self.same_day.toggled.connect(lambda checked: self.pay_date.setEnabled(not checked))
        pay_row = QHBoxLayout()
        pay_row.setSpacing(10)
        pay_row.addWidget(self.same_day)
        pay_row.addWidget(self.pay_date, 1)
        layout.addLayout(pay_row)

        layout.addWidget(_field_label("除权日前收盘价 (元)"))
        layout.addWidget(self.prev_spin)
        hint = QLabel("留空自动取本地行情的前一交易日收盘；本地没有行情时请手动填写。")
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons, ok = _dialog_buttons(self, "入账")
        ok.clicked.connect(self.accept)
        layout.addLayout(buttons)

    def values(self) -> dict:
        qd = self.ex_date.date()
        prev = self.prev_spin.value()
        values = {
            "ex_date": date(qd.year(), qd.month(), qd.day()),
            "cash_per_share_yuan": f"{self.cash_spin.value():.3f}",
            "bonus_ratio": f"{self.bonus_spin.value():.3f}",
            "transfer_ratio": f"{self.transfer_spin.value():.3f}",
            "prev_close_yuan": None if prev <= 0 else f"{prev:.3f}",
        }
        if not self.same_day.isChecked():
            pd = self.pay_date.date()
            values["pay_date"] = date(pd.year(), pd.month(), pd.day())
        return values


class ExportRangeDialog(QDialog):
    """选择记录导出的日期范围。"""

    def __init__(self, first_day: date, last_day: date, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出记录")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        head = QLabel(
            "导出该日期范围内的买卖明细（含决策记录，一笔一行）与公司行为"
            "（按除权日排序），两个工作表写入同一个 Excel 文件。"
        )
        head.setObjectName("cardHint")
        head.setWordWrap(True)
        layout.addWidget(head)

        self.start_edit = PaperDateEdit(
            QDate(first_day.year, first_day.month, first_day.day)
        )
        self.end_edit = PaperDateEdit(
            QDate(last_day.year, last_day.month, last_day.day)
        )
        for edit in (self.start_edit, self.end_edit):
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setCalendarPopup(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        for column, (text, widget) in enumerate(
            (("开始日期", self.start_edit), ("结束日期", self.end_edit))
        ):
            grid.addWidget(_field_label(text), 0, column)
            grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

        buttons, ok = _dialog_buttons(self, "下一步")
        ok.clicked.connect(self.accept)
        layout.addLayout(buttons)

    def values(self) -> tuple[date, date]:
        def to_date(edit) -> date:
            qd = edit.date()
            return date(qd.year(), qd.month(), qd.day())

        return to_date(self.start_edit), to_date(self.end_edit)


class NewAccountDialog(QDialog):
    """新建账户：名称 + 记账口径 + 期初资金（可为 0，先开户后入金）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建账户")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(40)
        self.name_edit.setPlaceholderText("如 长线组合 / 家人账户")

        self.mode_combo = PaperComboBox()
        for mode in LEDGER_MODES:
            self.mode_combo.addItem(LEDGER_MODE_LABELS[mode], mode)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.capital_spin = QDoubleSpinBox()
        self.capital_spin.setDecimals(2)
        self.capital_spin.setRange(0.0, 1_000_000_000.0)
        self.capital_spin.setGroupSeparatorShown(True)
        self.capital_spin.setValue(100_000.0)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        grid.addWidget(_field_label("账户名称"), 0, 0)
        grid.addWidget(_field_label("记账口径"), 0, 1)
        grid.addWidget(self.name_edit, 1, 0)
        grid.addWidget(self.mode_combo, 1, 1)
        self.capital_label = _field_label("初始本金 (元)")
        grid.addWidget(self.capital_label, 2, 0)
        grid.addWidget(self.capital_spin, 3, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.note = QLabel(
            "初始本金会记一笔期初入金；也可以填 0，之后在「账户设置」里入金。"
        )
        self.note.setObjectName("cardHint")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons, ok = _dialog_buttons(self, "新建账户")
        ok.clicked.connect(self._accept)
        layout.addLayout(buttons)
        self._on_mode_changed()

    def mode(self) -> str:
        return str(self.mode_combo.currentData() or LEDGER_MODE_SYSTEM)

    def _on_mode_changed(self, _index: int = 0) -> None:
        statement = self.mode() == LEDGER_MODE_STATEMENT
        self.capital_label.setText("期初现金 (元)" if statement else "初始本金 (元)")
        if statement:
            self.capital_spin.setValue(0.0)
            self.note.setText(
                "交割单口径：费率、红利与全部现金流水都以导入的交割单为准，"
                "手动记账将停用。期初现金通常填 0，银行入金会按交割单流水入账。"
            )
        else:
            self.note.setText(
                "系统计算口径：按「交易参数」实时计算佣金、印花税与过户费，"
                "并按持股期限计算红利税。初始本金会记一笔期初入金。"
            )

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            warn(self, "新建账户", "请先填写账户名称。")
            return
        self.accept()

    def values(self) -> tuple[str, int, str]:
        return (
            self.name_edit.text().strip(),
            int(round(self.capital_spin.value() * 100)),
            self.mode(),
        )


DIVIDEND_STATUS_LABELS = {
    "PENDING": "待缴",
    "WITHHELD": "已扣缴",
    "EXEMPT": "免税",
    "未参与": "未参与",
}

# 明细弹窗的列结构单独抽出来：行构造与表头共用同一份定义，避免再出现
# 「行多一个占位符 → 整行右移 → 已实现盈亏显示成净额」这类错列。
TRADE_DETAIL_HEADERS = (
    "日期",
    "方向",
    "数量",
    "成交价",
    "批次",
    "原始成本",
    "批次摊薄成本",
    "累计分红",
    "红利税",
    "红利状态",
    "佣金",
    "印花税",
    "过户费",
    "净额",
    "已实现盈亏",
)
TRADE_DETAIL_RIGHT = {2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14}

ACTION_DETAIL_HEADERS = (
    "批次",
    "除权日",
    "类型",
    "股数",
    "每股派息",
    "每股送股",
    "每股转增",
    "本批次红利(元)",
)
ACTION_DETAIL_RIGHT = {0, 3, 4, 5, 6, 7}


def _dividend_status_text(statuses: list[str]) -> str:
    """把批次的多个红利状态合并成一段文案，例如「免税 / 已扣缴」。"""
    labels = [DIVIDEND_STATUS_LABELS.get(code, code) for code in statuses if code]
    return " / ".join(labels)


class SymbolDetailDialog(QDialog):
    """单个标的的明细：买卖流水（右键可删改）+ 公司行为记录（只读）。

    非模态：主窗口每次数据刷新（_reload_all）都会回调 refresh_rows。
    """

    def __init__(self, window, symbol: str) -> None:
        super().__init__(window)
        self._window = window
        self.symbol = symbol
        self.setWindowTitle(f"{symbol} {window.name_of(symbol)} · 标的明细")
        self.resize(1180, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        trades_card, self.trades_view, self.trades_count = window._table_card(
            "买卖明细",
            "买入与卖出各自独立编号；累计分红为该批次历史累计现金分红（税前），"
            "红利税为该批次已扣缴税额，红利状态按当前持股期限判定",
        )
        self.trades_model = RecordsModel(
            TRADE_DETAIL_HEADERS,
            right_aligned=TRADE_DETAIL_RIGHT,
        )
        window._prepare_table(self.trades_view, self.trades_model)
        self.trades_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.trades_view.customContextMenuRequested.connect(self._on_trades_menu)
        layout.addWidget(trades_card, 3)

        review_card = QFrame()
        review_card.setObjectName("card")
        review_layout = QVBoxLayout(review_card)
        review_layout.setContentsMargins(18, 10, 18, 10)
        review_layout.setSpacing(4)
        review_title = QLabel("决策记录")
        review_title.setObjectName("cardTitle")
        self.review_hint = QLabel(
            "点击上方买卖明细中的一行查看该笔成交的决策记录"
            "（决策逻辑 / 计划内计划外 / 预期持有周期 / 是否改变买入逻辑，全部可选）"
        )
        self.review_hint.setObjectName("cardHint")
        self.review_hint.setWordWrap(True)
        self.review_text = QLabel("")
        self.review_text.setWordWrap(True)
        review_layout.addWidget(review_title)
        review_layout.addWidget(self.review_hint)
        review_layout.addWidget(self.review_text)
        layout.addWidget(review_card, 0)
        self.trades_view.selectionModel().currentRowChanged.connect(self._on_trade_selected)

        actions_card, self.actions_view, self.actions_count = window._table_card(
            "公司行为",
            "交割单红利与红股入账按批次拆分（只读）"
            " · 股数为该批次当次在册股数，同一除权日各行相加即当次合计"
            " · 红利状态与红利税见上方买卖明细"
            if window.service.is_statement_mode()
            else "除息派现与送转按批次拆分（只读，由买卖流水删改后自动重算）"
            " · 红利状态与红利税见上方买卖明细",
        )
        # 两种口径共用同一套列项；没有送转时自动隐藏送股/转增列
        self.actions_model = RecordsModel(
            ACTION_DETAIL_HEADERS,
            right_aligned=ACTION_DETAIL_RIGHT,
        )
        window._prepare_table(self.actions_view, self.actions_model)
        layout.addWidget(actions_card, 2)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("ghostButton")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setDefault(True)
        close.setAutoDefault(True)
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self.refresh_rows()

    # ------------------------------------------------------------ 数据
    def refresh_rows(self) -> None:
        service = self._window.service
        statement = service.is_statement_mode()
        trades = [t for t in service.portfolio.trades if t.symbol == self.symbol]
        book = service.portfolio.books.get(self.symbol)
        lots_by_trade: dict[int, object] = {}
        if book is not None:
            for lot in book.lots:
                if lot.open_trade_id:
                    lots_by_trade[lot.open_trade_id] = lot

        if statement:
            raw = service.statement_corporate_actions(self.symbol, batch_level=True)
        else:
            raw = service.lot_corporate_actions(self.symbol)

        # 每批次的历史红利事实：税前累计、已扣缴红利税、状态（清仓后仍保留）
        facts = service.dividend_facts(self.symbol)

        # 卖出单独编号 1、2、3……，与买入批次互不干扰
        sell_numbers: dict[int, int] = {}
        ordered = sorted(trades, key=lambda t: (t.trade_date, t.seq, t.trade_id))
        for number, trade in enumerate(
            (t for t in ordered if t.side == SIDE_SELL), start=1
        ):
            sell_numbers[trade.trade_id] = number

        rows = []
        for trade in reversed(ordered):
            if trade.side == SIDE_SELL:
                rows.append(
                    trade_record_row(
                        trade,
                        with_symbol=False,
                        batch_label=str(sell_numbers.get(trade.trade_id, "—")),
                    )
                )
                continue
            lot = lots_by_trade.get(trade.trade_id)
            fact = facts.get(lot.lot_id) if lot is not None else None
            rows.append(
                trade_record_row(
                    trade,
                    with_symbol=False,
                    lot=lot,
                    batch_label="" if lot is not None else "买入",
                    dividend_cents=fact["gross_cents"] if fact else None,
                    dividend_tax_cents=fact["tax_cents"] if fact else 0,
                    dividend_status=(
                        _dividend_status_text(fact["statuses"]) if fact else ""
                    ),
                )
            )
        self.trades_model.set_rows(rows)
        self.trades_count.setText(f"{len(rows)} 笔" if rows else "暂无记录")
        self._show_review(None)

        action_rows: list[RecordRow] = []
        for row in raw:
            cash = float(row["每股派息(元)"])
            bonus = float(row["每股送股"])
            transfer = float(row["每股转增"])
            gross = float(row["本批次红利(元)"])
            qty = int(row.get("股数") or 0)
            action_rows.append(
                RecordRow(
                    values=[
                        str(row["批次"]),
                        str(row["除权日"]),
                        str(row["类型"]),
                        f"{qty:,}" if qty else "—",
                        f"{cash:.3f}" if cash else "—",
                        f"{bonus:.3f}" if bonus else "—",
                        f"{transfer:.3f}" if transfer else "—",
                        f"{gross:,.2f}" if gross else "—",
                    ],
                    tones={7: "accent" if gross else "muted"},
                )
            )
        self.actions_model.set_rows(action_rows)
        # 每股派息 / 送股 / 转增：有值才显示，没有的列自动隐藏
        self.actions_view.setColumnHidden(4, not any(float(r["每股派息(元)"]) for r in raw))
        self.actions_view.setColumnHidden(5, not any(float(r["每股送股"]) for r in raw))
        self.actions_view.setColumnHidden(6, not any(float(r["每股转增"]) for r in raw))
        self.actions_count.setText(f"{len(action_rows)} 条" if action_rows else "暂无记录")

    # ------------------------------------------------------------ 复盘展示
    def _on_trade_selected(self, current, _previous=None) -> None:
        if not current.isValid():
            self._show_review(None)
            return
        rows = self.trades_model.rows()
        row = rows[current.row()] if current.row() < len(rows) else None
        self._show_review(row.payload if row is not None else None)

    def _show_review(self, trade) -> None:
        """把选中成交的可选决策字段拼接成一段可读文本。"""
        if trade is None:
            self.review_text.setText("点击上方买卖明细中的一行，查看该笔成交的决策记录。")
            return
        lines: list[str] = []
        logic = getattr(trade, "trade_logic", "")
        if logic:
            lines.append(f"决策逻辑：{logic}")
        meta: list[str] = []
        if getattr(trade, "plan_type", ""):
            meta.append(f"计划：{trade.plan_type}")
        if getattr(trade, "hold_period", ""):
            meta.append(f"预期持有：{trade.hold_period}")
        if getattr(trade, "strategy_changed", ""):
            meta.append(f"改变买入逻辑：{trade.strategy_changed}")
        if meta:
            lines.append("　".join(meta))
        self.review_text.setText("\n".join(lines) if lines else "这笔成交未记录决策信息。")

    # ------------------------------------------------------------ 右键删改
    def _on_trades_menu(self, pos) -> None:
        index = self.trades_view.indexAt(pos)
        if not index.isValid():
            return
        trade = self.trades_model.rows()[index.row()].payload
        if trade is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction("修改…")
        delete_action = menu.addAction("删除")
        chosen = menu.exec(self.trades_view.viewport().mapToGlobal(pos))
        if chosen is edit_action:
            self._window.edit_trade(trade)
        elif chosen is delete_action:
            self._window.delete_trade(trade)
