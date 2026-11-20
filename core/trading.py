"""交易引擎：下单 -> 计费 -> 批次扣减 -> 现金记账，全链路整数分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from config.settings import (
    DEFAULT_DIVIDEND_TAX_CONFIG,
    DEFAULT_FEE_CONFIG,
    DEFAULT_INITIAL_CAPITAL_CENTS,
    DividendTaxConfig,
    FeeConfig,
)
from core.fees import (
    SIDE_BUY,
    SIDE_SELL,
    AssetType,
    FeeBreakdown,
    calculate_fees,
    classify_symbol,
    total_cost_of_buy,
)
from core.lots import Consumption, LotBook, SellResult
from core.money import prorate, qty_price_to_cents

ENTRY_TRADE_BUY = "TRADE_BUY"
ENTRY_TRADE_SELL = "TRADE_SELL"
ENTRY_DIVIDEND = "DIVIDEND"
ENTRY_DIVIDEND_TAX = "DIVIDEND_TAX"
ENTRY_BONUS_RESIDUE = "BONUS_RESIDUE"
ENTRY_DEPOSIT = "DEPOSIT"


@dataclass
class TradeRecord:
    trade_id: int
    trade_date: date
    symbol: str
    side: str
    asset_type: str
    qty: int
    price_mills: int
    gross_cents: int
    commission_cents: int
    stamp_tax_cents: int
    transfer_fee_cents: int
    dividend_tax_cents: int
    net_cash_cents: int          # 买入为负（流出），卖出为正（流入）
    realized_pnl_cents: int      # 仅卖出有值
    seq: int = 0                 # 交割单原始行号：同日事件的先后顺序
    memo: str = ""               # 交割单原始摘要（红股/送股/转增等）
    # 交易复盘：全部可选，空字符串 / 0 表示未填写，不参与任何账务计算
    trade_logic: str = ""        # 买卖逻辑
    target_price_mills: int = 0  # 目标价（毫），0 表示未填
    plan_type: str = ""          # 计划内 / 计划外
    hold_period: str = ""        # 预期持有周期
    sell_reason: str = ""        # 卖出原因
    strategy_changed: str = ""   # 是否中途改变策略

    @property
    def fee_total_cents(self) -> int:
        return self.commission_cents + self.stamp_tax_cents + self.transfer_fee_cents


@dataclass
class CashEntry:
    entry_id: int
    entry_date: date
    amount_cents: int
    kind: str
    ref_id: int
    memo: str = ""
    seq: int = 0                 # 交割单原始行号：同日事件的先后顺序
    qty: int = 0                 # 红利入账的成交数量：当次实际在册股数


@dataclass
class DividendRecord:
    """批次级红利记录：除息日登记，卖出时按批次持股期限补扣。"""

    record_id: int
    symbol: str
    lot_id: int
    open_date: date
    ex_date: date
    qty: int
    gross_cents: int
    remaining_gross_cents: int
    tax_rate: Decimal = Decimal("0")
    tax_cents: int = 0
    status: str = "PENDING"      # PENDING / WITHHELD / EXEMPT


@dataclass
class Portfolio:
    """单账户组合：现金 + 各标的批次队列 + 全量流水。"""

    initial_cash_cents: int = DEFAULT_INITIAL_CAPITAL_CENTS
    fee_config: FeeConfig = DEFAULT_FEE_CONFIG
    tax_config: DividendTaxConfig = DEFAULT_DIVIDEND_TAX_CONFIG
    cash_cents: int = field(init=False)
    books: dict[str, LotBook] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    ledger: list[CashEntry] = field(default_factory=list)
    dividends: list[DividendRecord] = field(default_factory=list)
    consumptions: list[tuple[int, Consumption]] = field(default_factory=list)

    _next_trade_id: int = field(default=1, init=False)
    _next_entry_id: int = field(default=1, init=False)
    _next_dividend_id: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        self.cash_cents = 0
        if self.initial_cash_cents > 0:
            self._record_cash(
                date(1900, 1, 1), self.initial_cash_cents, ENTRY_DEPOSIT, 0, "初始入金"
            )

    # ---------- 内部 ----------
    def _record_cash(
        self,
        entry_date: date,
        amount: int,
        kind: str,
        ref_id: int,
        memo: str = "",
        seq: int = 0,
        qty: int = 0,
    ) -> CashEntry:
        entry = CashEntry(self._next_entry_id, entry_date, amount, kind, ref_id, memo, seq, qty)
        self._next_entry_id += 1
        self.cash_cents += amount
        self.ledger.append(entry)
        return entry

    def book(self, symbol: str) -> LotBook:
        """取得（必要时创建）某标的的批次账本。"""
        if symbol not in self.books:
            self.books[symbol] = LotBook(symbol, classify_symbol(symbol))
        return self.books[symbol]

    def position_qty(self, symbol: str) -> int:
        book = self.books.get(symbol)
        return book.total_qty if book else 0

    # ---------- 买入 ----------
    def buy(
        self,
        symbol: str,
        trade_date: date,
        qty: int,
        price_mills: int,
        *,
        fees: FeeBreakdown | None = None,
        net_cash_cents: int | None = None,
        seq: int = 0,
        memo: str = "",
    ) -> TradeRecord:
        """买入。

        `fees` / `net_cash_cents` 仅供「交割单口径」使用：直接采用券商交割单上的
        真实费用与现金流，不再经费率引擎重算。`seq` 记录原始行号，保证同日回放顺序。
        """
        book = self.book(symbol)
        if fees is None:
            fees = calculate_fees(
                SIDE_BUY, qty, price_mills, symbol, self.fee_config, book.asset_type
            )
        gross = qty_price_to_cents(qty, price_mills)
        total_cost = (
            abs(net_cash_cents)
            if net_cash_cents is not None
            else total_cost_of_buy(qty, price_mills, fees)
        )

        if total_cost > self.cash_cents:
            raise ValueError(
                f"现金不足：需 {total_cost} 分，可用 {self.cash_cents} 分"
            )

        trade = TradeRecord(
            trade_id=self._next_trade_id,
            trade_date=trade_date,
            symbol=symbol,
            side=SIDE_BUY,
            asset_type=book.asset_type.value,
            qty=qty,
            price_mills=price_mills,
            gross_cents=gross,
            commission_cents=fees.commission_cents,
            stamp_tax_cents=fees.stamp_tax_cents,
            transfer_fee_cents=fees.transfer_fee_cents,
            dividend_tax_cents=0,
            net_cash_cents=-total_cost,
            realized_pnl_cents=0,
            seq=seq,
            memo=memo,
        )
        self._next_trade_id += 1

        book.buy(trade_date, qty, total_cost, trade.trade_id, price_mills)
        self._record_cash(
            trade_date, -total_cost, ENTRY_TRADE_BUY, trade.trade_id, symbol, seq=seq
        )
        self.trades.append(trade)
        return trade

    # ---------- 卖出 ----------
    def sell(
        self,
        symbol: str,
        trade_date: date,
        qty: int,
        price_mills: int,
        *,
        fees: FeeBreakdown | None = None,
        net_cash_cents: int | None = None,
        seq: int = 0,
    ) -> TradeRecord:
        """卖出；`fees` / `net_cash_cents` / `seq` 的含义同 `buy`（交割单口径专用）。"""
        book = self.book(symbol)
        if qty > book.total_qty:
            raise ValueError(f"{symbol} 可卖数量不足：持有 {book.total_qty}，试图卖出 {qty}")

        if fees is None:
            fees = calculate_fees(
                SIDE_SELL, qty, price_mills, symbol, self.fee_config, book.asset_type
            )
        gross = qty_price_to_cents(qty, price_mills)

        if net_cash_cents is not None:
            # 交割单口径：以真实到账金额为准，用「成交额-到账」反推有效费用。
            # 有些券商把印花税、过户费并进「手续费」列，若再按分项费用扣一次，
            # 已实现盈亏会重复扣费；这里只认发生金额，与券商 APP 保持一致。
            net_in = net_cash_cents
            effective_fee = max(0, gross - net_in)
        else:
            net_in = gross - fees.total_cents
            effective_fee = fees.total_cents

        result: SellResult = book.sell(
            sell_date=trade_date,
            qty=qty,
            gross_cents=gross,
            fee_cents=effective_fee,
            tax_config=self.tax_config,
        )

        trade = TradeRecord(
            trade_id=self._next_trade_id,
            trade_date=trade_date,
            symbol=symbol,
            side=SIDE_SELL,
            asset_type=book.asset_type.value,
            qty=qty,
            price_mills=price_mills,
            gross_cents=gross,
            commission_cents=fees.commission_cents,
            stamp_tax_cents=fees.stamp_tax_cents,
            transfer_fee_cents=fees.transfer_fee_cents,
            dividend_tax_cents=result.dividend_tax_cents,
            net_cash_cents=net_in,
            realized_pnl_cents=result.realized_pnl_cents,
            seq=seq,
        )
        self._next_trade_id += 1

        # 成交净流入；红利税仍是独立的一笔支出（交割单口径下税额已含在现金流里）
        self._record_cash(
            trade_date,
            net_in,
            ENTRY_TRADE_SELL,
            trade.trade_id,
            symbol,
            seq=seq,
        )
        if net_cash_cents is None and result.dividend_tax_cents > 0:
            self._record_cash(
                trade_date,
                -result.dividend_tax_cents,
                ENTRY_DIVIDEND_TAX,
                trade.trade_id,
                f"{symbol} 卖出补扣红利税",
                seq=seq,
            )

        for consumption in result.consumptions:
            self.consumptions.append((trade.trade_id, consumption))
            self._settle_dividend_records(symbol, consumption)

        self.trades.append(trade)
        return trade

    def _settle_dividend_records(self, symbol: str, consumption: Consumption) -> None:
        """把本次消耗对应的待缴红利本金结转出账，并按金额比例分摊已扣税额。"""
        pending = [
            record
            for record in self.dividends
            if record.symbol == symbol
            and record.lot_id == consumption.lot_id
            and record.status == "PENDING"
        ]
        target_gross = consumption.dividend_gross_cents
        if not pending or target_gross <= 0:
            return

        total_remaining = sum(record.remaining_gross_cents for record in pending)
        if total_remaining <= 0:
            return

        settled_gross = 0
        allocated_tax = 0
        for index, record in enumerate(pending):
            if settled_gross >= target_gross:
                break
            is_last = index == len(pending) - 1
            want = target_gross - settled_gross
            if is_last:
                share = min(want, record.remaining_gross_cents)
            else:
                share = min(prorate(target_gross, record.remaining_gross_cents, total_remaining), want)
            if share <= 0:
                continue

            if is_last or settled_gross + share >= target_gross:
                tax_share = consumption.dividend_tax_cents - allocated_tax
            else:
                tax_share = prorate(consumption.dividend_tax_cents, share, target_gross)
            tax_share = max(0, min(tax_share, consumption.dividend_tax_cents - allocated_tax))

            record.remaining_gross_cents -= share
            record.tax_cents += tax_share
            record.tax_rate = consumption.tax_rate
            if record.remaining_gross_cents == 0:
                record.status = "EXEMPT" if consumption.tax_rate == 0 else "WITHHELD"

            settled_gross += share
            allocated_tax += tax_share

    # ---------- 估值 ----------
    def market_value_cents(self, prices: dict[str, int]) -> int:
        """按收盘价（毫）计算持仓市值；缺失价格的标的按成本估值并标记。"""
        total = 0
        for symbol, book in self.books.items():
            if book.total_qty == 0:
                continue
            price = prices.get(symbol)
            if price is None:
                total += book.cost_remaining_cents
                continue
            total += qty_price_to_cents(book.total_qty, price)
        return total

    def total_asset_cents(self, prices: dict[str, int]) -> int:
        return self.cash_cents + self.market_value_cents(prices)

    # ---------- 供公司行为模块调用的记账入口 ----------
    def post_cash(
        self,
        entry_date: date,
        amount_cents: int,
        kind: str,
        ref_id: int,
        memo: str = "",
        seq: int = 0,
        qty: int = 0,
    ) -> CashEntry:
        return self._record_cash(entry_date, amount_cents, kind, ref_id, memo, seq, qty)

    def add_dividend_record(
        self,
        symbol: str,
        lot_id: int,
        open_date: date,
        ex_date: date,
        qty: int,
        gross_cents: int,
        *,
        status: str = "PENDING",
        tax_rate: Decimal = Decimal("0"),
    ) -> DividendRecord:
        """登记一条批次红利；免税红利直接记 EXEMPT，不再进入待缴队列。"""
        exempt = status == "EXEMPT"
        record = DividendRecord(
            record_id=self._next_dividend_id,
            symbol=symbol,
            lot_id=lot_id,
            open_date=open_date,
            ex_date=ex_date,
            qty=qty,
            gross_cents=gross_cents,
            remaining_gross_cents=0 if exempt else gross_cents,
            tax_rate=tax_rate,
            status=status,
        )
        self._next_dividend_id += 1
        self.dividends.append(record)
        return record
