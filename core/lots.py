"""持仓批次账本（FIFO 先进先出）。

设计要点：
- 每笔买入生成独立批次，永不合并加权平均；
- 卖出按 (买入日, lot_id) 升序消耗，逐批记录成本释放与已实现盈亏；
- 送转股按批次归属增加股数，成本总额不变 -> 每股成本自动摊薄；
- 红利税待扣额挂在批次上，卖出时按剩余股数比例分摊结算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from config.settings import DEFAULT_DIVIDEND_TAX_CONFIG, DividendTaxConfig
from core.fees import AssetType
from core.money import MILLS_PER_CENT, floor_int, prorate, round_half_up
from core.tax import dividend_tax_on, holding_period_bracket


@dataclass
class Lot:
    """一个建仓批次。open_date 是红利税时间戳的唯一来源。"""

    lot_id: int
    symbol: str
    open_date: date
    qty_opened: int
    qty_remaining: int
    cost_cents: int                    # 初始总成本（含买入费用）
    cost_remaining_cents: int          # 尚未结转的总成本
    buy_price_mills: int = 0           # 买入原价（毫/股），永久固定，不随送转变化
    pending_div_cents: int = 0         # 已到账、待补扣税的红利本金（分）
    pending_div_shares_basis: int = 0  # 除息日参与分红的持股数（审计用）
    received_div_cents: int = 0        # 剩余股数累计已收现金分红（净额，卖出按比例释放）
    open_trade_id: int = 0             # 来源买入流水号（持久化用）

    @property
    def is_closed(self) -> bool:
        return self.qty_remaining == 0

    @property
    def unit_cost_mills(self) -> int:
        """原始成本（毫/股）：买入真实总成本 ÷ 当前股数，送转后自动摊薄。

        分红与部分卖出都不改变它，是「这批买入到底花了多少钱」的锁定口径。
        """
        if self.qty_remaining <= 0:
            return 0
        return round_half_up(
            Decimal(self.cost_remaining_cents * MILLS_PER_CENT) / Decimal(self.qty_remaining)
        )

    @property
    def original_cost_mills(self) -> int:
        """原始成本（毫/股）别名：供界面双口径展示使用。"""
        return self.unit_cost_mills

    @property
    def diluted_cost_mills(self) -> int:
        """摊薄成本（毫/股）：原始成本扣除剩余股数累计现金分红。

        分红回本进度 = 1 − 摊薄成本 ÷ 原始成本；分红超过成本时可以为负。
        """
        if self.qty_remaining <= 0:
            return 0
        net_cost = self.cost_remaining_cents - self.received_div_cents
        return round_half_up(
            Decimal(net_cost * MILLS_PER_CENT) / Decimal(self.qty_remaining)
        )


@dataclass
class Consumption:
    """单批次卖出扣减明细，构成可逐笔复盘的审计链。"""

    lot_id: int
    open_date: date
    qty: int
    gross_cents: int
    fee_cents: int
    cost_released_cents: int
    dividend_gross_cents: int
    dividend_tax_cents: int
    tax_rate: Decimal
    tax_bracket: str
    realized_pnl_cents: int


@dataclass
class SellResult:
    qty: int
    gross_cents: int
    fee_cents: int
    dividend_tax_cents: int
    net_cash_cents: int
    realized_pnl_cents: int
    consumptions: list[Consumption] = field(default_factory=list)


@dataclass
class BonusResult:
    """送转结果：新增股数 + 碎股现金补偿。"""

    qty_added: int
    residue_cents: int
    ratio: Decimal


class LotBook:
    """单只标的的批次队列。纯内存、无 IO，可独立单测。"""

    def __init__(self, symbol: str, asset_type: AssetType = AssetType.STOCK) -> None:
        self.symbol = symbol
        self.asset_type = asset_type
        self.lots: list[Lot] = []
        self._next_lot_id = 1

    # ---------- 查询 ----------
    @property
    def total_qty(self) -> int:
        return sum(lot.qty_remaining for lot in self.lots)

    @property
    def open_lots(self) -> list[Lot]:
        return [lot for lot in self.lots if lot.qty_remaining > 0]

    @property
    def cost_remaining_cents(self) -> int:
        return sum(lot.cost_remaining_cents for lot in self.lots)

    @property
    def pending_dividend_cents(self) -> int:
        return sum(lot.pending_div_cents for lot in self.lots)

    def open_lots_as_of(self, day: date) -> list[Lot]:
        """除权除息日的在册批次：买入日必须早于该日（除权日当天买入不享受）。"""
        return [lot for lot in self.open_lots if lot.open_date < day]

    def fifo_order(self) -> list[Lot]:
        """FIFO 消耗顺序：买入日升序，同日按批次号升序。"""
        return sorted(self.lots, key=lambda lot: (lot.open_date, lot.lot_id))

    # ---------- 建仓 ----------
    def buy(
        self,
        trade_date: date,
        qty: int,
        total_cost_cents: int,
        open_trade_id: int = 0,
        buy_price_mills: int = 0,
    ) -> Lot:
        if qty <= 0:
            raise ValueError("买入数量必须为正")
        if total_cost_cents < 0:
            raise ValueError("买入成本不能为负")
        lot = Lot(
            lot_id=self._next_lot_id,
            symbol=self.symbol,
            open_date=trade_date,
            qty_opened=qty,
            qty_remaining=qty,
            cost_cents=total_cost_cents,
            cost_remaining_cents=total_cost_cents,
            buy_price_mills=int(buy_price_mills),
            open_trade_id=open_trade_id,
        )
        self._next_lot_id += 1
        self.lots.append(lot)
        return lot

    # ---------- 卖出（FIFO） ----------
    def sell(
        self,
        sell_date: date,
        qty: int,
        gross_cents: int,
        fee_cents: int,
        tax_config: DividendTaxConfig = DEFAULT_DIVIDEND_TAX_CONFIG,
    ) -> SellResult:
        if qty <= 0:
            raise ValueError("卖出数量必须为正")
        if qty > self.total_qty:
            raise ValueError(f"可卖数量不足：持有 {self.total_qty}，试图卖出 {qty}")

        remaining_qty = qty
        remaining_gross = gross_cents
        remaining_fee = fee_cents
        total_tax = 0
        total_pnl = 0
        consumptions: list[Consumption] = []

        for lot in self.fifo_order():
            if remaining_qty == 0:
                break
            if lot.qty_remaining == 0:
                continue

            take = min(lot.qty_remaining, remaining_qty)
            is_final_piece = take == remaining_qty

            if is_final_piece:
                alloc_gross, alloc_fee = remaining_gross, remaining_fee
            else:
                alloc_gross = prorate(gross_cents, take, qty)
                alloc_fee = prorate(fee_cents, take, qty)

            if take == lot.qty_remaining:
                cost_released = lot.cost_remaining_cents
                div_gross = lot.pending_div_cents
                div_received_released = lot.received_div_cents
            else:
                cost_released = prorate(lot.cost_remaining_cents, take, lot.qty_remaining)
                div_gross = prorate(lot.pending_div_cents, take, lot.qty_remaining)
                div_received_released = prorate(
                    lot.received_div_cents, take, lot.qty_remaining
                )

            rate, bracket = holding_period_bracket(
                lot.open_date, sell_date, self.asset_type, tax_config
            )
            div_tax = dividend_tax_on(div_gross, rate)
            pnl = alloc_gross - alloc_fee - cost_released - div_tax

            lot.qty_remaining -= take
            lot.cost_remaining_cents -= cost_released
            lot.pending_div_cents -= div_gross
            lot.received_div_cents -= div_received_released
            lot.pending_div_shares_basis = max(
                0, lot.pending_div_shares_basis - (take if div_gross else 0)
            )
            if lot.qty_remaining == 0:
                lot.cost_remaining_cents = 0
                lot.pending_div_cents = 0
                lot.received_div_cents = 0
                lot.pending_div_shares_basis = 0

            consumptions.append(
                Consumption(
                    lot_id=lot.lot_id,
                    open_date=lot.open_date,
                    qty=take,
                    gross_cents=alloc_gross,
                    fee_cents=alloc_fee,
                    cost_released_cents=cost_released,
                    dividend_gross_cents=div_gross,
                    dividend_tax_cents=div_tax,
                    tax_rate=rate,
                    tax_bracket=bracket,
                    realized_pnl_cents=pnl,
                )
            )

            remaining_qty -= take
            remaining_gross -= alloc_gross
            remaining_fee -= alloc_fee
            total_tax += div_tax
            total_pnl += pnl

        if remaining_qty != 0:
            raise RuntimeError("FIFO 消耗异常：仍有未匹配数量")

        return SellResult(
            qty=qty,
            gross_cents=gross_cents,
            fee_cents=fee_cents,
            dividend_tax_cents=total_tax,
            net_cash_cents=gross_cents - fee_cents - total_tax,
            realized_pnl_cents=total_pnl,
            consumptions=consumptions,
        )

    # ---------- 现金分红 ----------
    def credit_dividend(self, cash_per_share_mills: int) -> int:
        """除息日按各批次持股数登记红利本金（全额到账，暂不扣税）。"""
        if cash_per_share_mills <= 0:
            return 0
        total = 0
        for lot in self.open_lots:
            gross = round_half_up(
                Decimal(lot.qty_remaining) * Decimal(cash_per_share_mills) / Decimal(10)
            )
            lot.pending_div_cents += gross
            lot.received_div_cents += gross
            lot.pending_div_shares_basis += lot.qty_remaining
            total += gross
        return total

    # ---------- 送转股 ----------
    def apply_bonus_shares(
        self, ratio: Decimal, ex_price_mills: int, lots: list[Lot] | None = None
    ) -> BonusResult:
        """送转：股数按比例增加，成本总额不变（每股成本自动摊薄）。

        不足 1 股的零碎部分，按除权参考价折成现金补偿，保证市值守恒。
        `lots` 默认作用于全部未平仓批次；自动同步历史事件时只传在册批次。
        """
        if ratio < 0:
            raise ValueError("送转比例不能为负")
        if ratio == 0:
            return BonusResult(0, 0, Decimal("0"))

        qty_added = 0
        residue_cents = 0
        factor = Decimal("1") + Decimal(ratio)

        for lot in self.open_lots if lots is None else lots:
            exact = Decimal(lot.qty_remaining) * factor
            new_qty = floor_int(exact)
            residue_shares = exact - Decimal(new_qty)
            added = new_qty - lot.qty_remaining

            lot.qty_opened += added
            lot.qty_remaining = new_qty
            qty_added += added

            if residue_shares > 0:
                # 碎股以现金补偿：碎股 × 除权参考价
                residue_cents += round_half_up(
                    residue_shares * Decimal(ex_price_mills) / Decimal(10)
                )

        return BonusResult(qty_added, residue_cents, Decimal(ratio))
