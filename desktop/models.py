"""QAbstractTableModel 实现：持仓明细主表 + 通用流水/批次表。

模型只负责「把服务层数据变成可显示的单元格」，不做任何账务计算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from core.fees import SIDE_SELL
from desktop.qt import QAbstractTableModel, QBrush, QColor, QModelIndex, Qt
from desktop.theme import TONE_COLORS


@dataclass
class HoldingRow:
    """一行持仓：原始/摊薄成本与盈亏均已由服务层算好，这里只做展示。"""

    symbol: str
    name: str
    qty: int
    original_cost: Optional[float]
    diluted_cost: Optional[float]
    dividend: Optional[float]
    price: Optional[float]
    market_value: Optional[float]
    pnl: Optional[float]
    pnl_ratio: Optional[float]


@dataclass
class RecordRow:
    """通用记录行：values 已格式化，tones 指定某列的颜色语义。

    payload 可选地挂上原始业务对象（如 TradeRecord），供右键菜单定位删改目标。
    """

    values: list[str]
    tones: dict[int, str] = field(default_factory=dict)
    payload: object = None


def trade_record_row(
    trade,
    name: str = "",
    with_symbol: bool = True,
    *,
    lot=None,
    batch_label: str = "",
    dividend_cents: int | None = None,
    dividend_tax_cents: int | None = None,
    dividend_status: str = "",
) -> "RecordRow":
    """把一笔成交格式化成表格行；payload 带回原始成交对象，供删改使用。

    with_symbol=False 用于单标的明细弹窗：代码/名称两列省掉，避免冗余。
    `lot` 是该笔买入对应的批次：传入后会插入「批次 / 原始成本 / 批次摊薄成本 /
    累计分红 / 红利税 / 红利状态」六列；卖出传 `batch_label="1"` 之类的独立编号，
    这六列留空，与买入批次互不干扰。`dividend_cents` 为该批次历史累计现金分红
    （税前），`dividend_tax_cents` 为该批次累计已扣红利税，`dividend_status`
    为红利状态文案（待缴 / 已扣缴 / 免税 / 未参与）。
    """
    pnl = trade.realized_pnl_cents / 100
    is_sell = trade.side == SIDE_SELL
    values = [trade.trade_date.isoformat(), "卖出" if is_sell else "买入"]
    tones = {1: "up" if is_sell else "accent"}
    if with_symbol:
        values += [trade.symbol, name or "—"]
    values += [
        f"{trade.qty:,}",
        f"{trade.price_mills / 1000:.3f}",
    ]
    if lot is not None or batch_label:
        if lot is not None:
            original = (
                f"{lot.cost_cents / lot.qty_opened / 100:.3f}" if lot.qty_opened else "—"
            )
            diluted = (
                f"{lot.diluted_cost_mills / 1000:.3f}" if lot.qty_remaining > 0 else "—"
            )
            dividend = (
                lot.received_div_cents if dividend_cents is None else dividend_cents
            )
            tax = dividend_tax_cents or 0
            values += [
                str(lot.lot_id),
                original,
                diluted,
                f"{dividend / 100:,.2f}",
                f"{tax / 100:,.2f}" if tax else "—",
                dividend_status or "—",
            ]
        else:
            # 表头批次块只有六列（批次/原始成本/批次摊薄成本/累计分红/红利税/红利状态）
            values += [batch_label, "—", "—", "—", "—", "—"]
    values += [
        f"{trade.commission_cents / 100:,.2f}",
        f"{trade.stamp_tax_cents / 100:,.2f}",
        f"{trade.transfer_fee_cents / 100:,.2f}",
        f"{trade.net_cash_cents / 100:+,.2f}",
        f"{pnl:+,.2f}" if is_sell else "—",
    ]
    net_col = len(values) - 2
    tones[net_col] = "muted"
    tones[net_col + 1] = (
        ("up" if pnl > 0 else "down" if pnl < 0 else "muted") if is_sell else "muted"
    )
    return RecordRow(values=values, tones=tones, payload=trade)


def _fmt(value: Optional[float], digits: int, signed: bool = False) -> str:
    if value is None:
        return "—"
    if signed and value != 0:
        return f"{value:+,.{digits}f}"
    return f"{value:,.{digits}f}"


def _text_key(text: str) -> tuple:
    """排序键：数字按数值、日期按时间、其余按文本；空值排最后。"""
    raw = (text or "").strip()
    if raw in ("", "—", "-"):
        return (3, 0)
    numeric = raw.replace(",", "").replace("%", "")
    if numeric.startswith("+"):
        numeric = numeric[1:]
    try:
        return (0, float(numeric))
    except ValueError:
        pass
    try:
        return (1, date.fromisoformat(raw).toordinal())
    except ValueError:
        return (2, raw)


class HoldingsModel(QAbstractTableModel):
    """持仓明细表：代码、名称、数量、原始成本、摊薄成本、累计分红、现价、市值、盈亏、盈亏率。"""

    HEADERS = (
        "证券代码", "证券名称", "持仓数量", "原始成本", "摊薄成本",
        "累计分红", "现价", "市值", "盈亏", "盈亏率",
    )
    RIGHT_ALIGNED = {2, 3, 4, 5, 6, 7, 8, 9}
    TINTED = {8, 9}

    def __init__(self, rows: Optional[list[HoldingRow]] = None) -> None:
        super().__init__()
        self._rows: list[HoldingRow] = list(rows or [])
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

    # ---------- Qt 接口 ----------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (
                Qt.AlignmentFlag.AlignRight
                if section in self.RIGHT_ALIGNED
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return (
                row.symbol,
                row.name,
                f"{row.qty:,}",
                _fmt(row.original_cost, 3),
                _fmt(row.diluted_cost, 3),
                _fmt(row.dividend, 2),
                _fmt(row.price, 3),
                _fmt(row.market_value, 2),
                _fmt(row.pnl, 2, signed=True),
                "—" if row.pnl_ratio is None else f"{row.pnl_ratio:+.2f}%",
            )[column]

        if role == Qt.ItemDataRole.ForegroundRole and column in self.TINTED:
            if row.pnl is None or row.pnl == 0:
                return QBrush(QColor(TONE_COLORS["muted"]))
            tone = "up" if row.pnl > 0 else "down"
            return QBrush(QColor(TONE_COLORS[tone]))

        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (
                Qt.AlignmentFlag.AlignRight
                if column in self.RIGHT_ALIGNED
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(align | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 3:
                return "原始成本：剩余批次的买入真实总成本（含手续费）÷ 当前股数，只在送转时摊薄"
            if column == 4:
                return (
                    "摊薄成本（券商口径）=（累计买入含费 − 累计卖出净额 − 累计税前分红）"
                    "÷ 当前股数，含已实现盈亏，与券商 App 的成本价一致；"
                    "整仓清空后重新开仓时上一轮盈亏不再摊入"
                )
            if column == 5:
                return "该标的累计已收税前现金分红（券商成本价口径，不扣红利税）"
            if column == 8:
                return "持仓盈亏 = 市值 − 摊薄成本 × 股数，含已实现盈亏"
        return None

    # ---------- 排序 ----------
    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:  # noqa: N802
        self.beginResetModel()
        self._sort_column = int(column)
        self._sort_order = order
        self._apply_sort()
        self.endResetModel()

    @staticmethod
    def _key(row: "HoldingRow", column: int) -> tuple:
        if column == 0:
            return _text_key(row.symbol)
        if column == 1:
            return _text_key(row.name)
        value = (
            row.qty, row.original_cost, row.diluted_cost, row.dividend,
            row.price, row.market_value, row.pnl, row.pnl_ratio,
        )[column - 2]
        return (1, 0) if value is None else (0, value)

    def _apply_sort(self) -> None:
        if self._sort_column < 0:
            return
        column = self._sort_column
        self._rows.sort(
            key=lambda row: self._key(row, column),
            reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
        )

    # ---------- 数据 ----------
    def set_rows(self, rows: list[HoldingRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._apply_sort()
        self.endResetModel()

    def rows(self) -> list[HoldingRow]:
        return list(self._rows)


class RecordsModel(QAbstractTableModel):
    """通用只读表：用于批次、公司行为、最近成交。"""

    def __init__(
        self,
        headers: tuple[str, ...],
        right_aligned: Optional[set[int]] = None,
    ) -> None:
        super().__init__()
        self._headers = tuple(headers)
        self._right = set(right_aligned or ())
        self._rows: list[RecordRow] = []
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

    def set_headers(
        self, headers: tuple[str, ...], right_aligned: Optional[set[int]] = None
    ) -> None:
        """切换列结构（如记账口径不同导致公司行为列含义不同）。"""
        self.beginResetModel()
        self._headers = tuple(headers)
        self._right = set(right_aligned or ())
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._headers[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (
                Qt.AlignmentFlag.AlignRight
                if section in self._right
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return row.values[column] if column < len(row.values) else ""
        if role == Qt.ItemDataRole.ForegroundRole:
            tone = row.tones.get(column)
            return QBrush(QColor(TONE_COLORS.get(tone, TONE_COLORS["ink"]))) if tone else None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (
                Qt.AlignmentFlag.AlignRight
                if column in self._right
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        return None

    # ---------- 排序 ----------
    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:  # noqa: N802
        self.beginResetModel()
        self._sort_column = int(column)
        self._sort_order = order
        self._apply_sort()
        self.endResetModel()

    def _apply_sort(self) -> None:
        if self._sort_column < 0:
            return
        column = self._sort_column

        def key(row: RecordRow) -> tuple:
            text = row.values[column] if column < len(row.values) else ""
            return _text_key(text)

        self._rows.sort(
            key=key, reverse=self._sort_order == Qt.SortOrder.DescendingOrder
        )

    def set_rows(self, rows: list[RecordRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._apply_sort()
        self.endResetModel()

    def rows(self) -> list[RecordRow]:
        return list(self._rows)
