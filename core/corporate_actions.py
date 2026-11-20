"""公司行为：现金分红、送转股、除权除息。

除权参考价严格采用 A 股官方公式：

    P_ex = (前收盘价 - 每股现金红利) / (1 + 每股送转比例)

并在事件落账后强制校验市值守恒：

    除权前总资产 == 除权后市值(按 P_ex) + 派发现金 + 碎股补偿

不等则抛错整笔回滚，从机制上杜绝「坑 1：除权日净值断裂」。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from core.money import qty_price_to_cents, round_half_up
from core.tax import holding_period_bracket

if TYPE_CHECKING:  # 仅类型引用，避免与 trading 循环导入
    from core.trading import Portfolio

ENTRY_DIVIDEND = "DIVIDEND"
ENTRY_BONUS_RESIDUE = "BONUS_RESIDUE"


class ConservationError(ValueError):
    """除权市值不守恒时抛出，调用方应回滚事务。"""


@dataclass(frozen=True)
class CorporateAction:
    """一次公司行为事件。比例均为「每股」口径，如 10 送 3 记 0.3。"""

    symbol: str
    ex_date: date
    cash_per_share_mills: int = 0          # 每股现金红利（毫/股）
    bonus_ratio: Decimal = Decimal("0")    # 每股送股比例
    transfer_ratio: Decimal = Decimal("0") # 每股转增比例
    pay_date: date | None = None

    @property
    def share_ratio(self) -> Decimal:
        return Decimal(self.bonus_ratio) + Decimal(self.transfer_ratio)

    @property
    def has_dividend(self) -> bool:
        return self.cash_per_share_mills > 0

    @property
    def has_share_growth(self) -> bool:
        return self.share_ratio > 0


@dataclass
class CorporateActionResult:
    symbol: str
    ex_date: date
    ex_price_mills: int
    dividend_gross_cents: int
    qty_before: int
    qty_after: int
    qty_added: int
    residue_cents: int
    pre_value_cents: int
    post_value_cents: int
    drift_cents: int
    allowance_cents: int


def ex_rights_price(
    prev_close_mills: int,
    cash_per_share_mills: int,
    share_ratio: Decimal,
) -> int:
    """A 股官方除权（除息）参考价，返回毫。"""
    if prev_close_mills <= 0:
        raise ValueError("前收盘价必须为正")
    if cash_per_share_mills < 0:
        raise ValueError("每股红利不能为负")
    factor = Decimal("1") + Decimal(share_ratio)
    if factor <= 0:
        raise ValueError("送转比例非法")
    value = (Decimal(prev_close_mills) - Decimal(cash_per_share_mills)) / factor
    if value < 0:
        raise ValueError("每股红利超过前收盘价，除权价将为负")
    return round_half_up(value)


def check_conservation(
    symbol: str,
    pre_value_cents: int,
    qty_after: int,
    ex_price_mills: int,
    dividend_gross_cents: int,
    residue_cents: int,
) -> tuple[int, int, int]:
    """校验除权前后市值守恒，返回 (后市值, 差额, 容许余量)。

    后市值 = 除权后股数 × 除权参考价 + 派发现金 + 碎股补偿
    """
    post_value = (
        qty_price_to_cents(qty_after, ex_price_mills) + dividend_gross_cents + residue_cents
    )
    drift = post_value - pre_value_cents
    allowance = max(1, qty_after // 20 + 2)
    if abs(drift) > allowance:
        raise ConservationError(
            f"{symbol} 除权市值断裂：前 {pre_value_cents} 分，后 {post_value} 分，"
            f"差额 {drift} 分，容许 {allowance} 分"
        )
    return post_value, drift, allowance


def apply_corporate_action(
    portfolio: "Portfolio",
    action: CorporateAction,
    prev_close_mills: int,
    eligible_on: date | None = None,
) -> CorporateActionResult:
    """执行分红/送转：先派现、后送转、最后校验守恒。

    `eligible_on` 为空时作用于全部未平仓批次（当日手动录入）；
    自动同步历史事件时传除权日，只让当时已持有的批次参与，避免把
    买入日之后发生的分红错误地补给后买入的批次。
    """
    book = portfolio.books.get(action.symbol)
    lots = [] if book is None else (
        book.open_lots if eligible_on is None else book.open_lots_as_of(eligible_on)
    )
    qty_before = sum(lot.qty_remaining for lot in lots)
    ex_price = ex_rights_price(
        prev_close_mills, action.cash_per_share_mills, action.share_ratio
    )

    if qty_before == 0:
        return CorporateActionResult(
            symbol=action.symbol,
            ex_date=action.ex_date,
            ex_price_mills=ex_price,
            dividend_gross_cents=0,
            qty_before=0,
            qty_after=0,
            qty_added=0,
            residue_cents=0,
            pre_value_cents=0,
            post_value_cents=0,
            drift_cents=0,
            allowance_cents=0,
        )

    pre_value = qty_price_to_cents(qty_before, prev_close_mills)

    # 1) 派现：全额到账；除权日已持股满 1 年（或 ETF）的批次当场免税，其余登记待缴
    dividend_total = 0
    if action.has_dividend:
        for lot in lots:
            gross = qty_price_to_cents(lot.qty_remaining, action.cash_per_share_mills)
            if gross <= 0:
                continue
            rate, _bracket = holding_period_bracket(
                lot.open_date, action.ex_date, book.asset_type, portfolio.tax_config
            )
            exempt = rate == 0
            if not exempt:
                lot.pending_div_cents += gross
                lot.pending_div_shares_basis += lot.qty_remaining
            # 双口径展示用：实际收到的现金分红永久累积在批次上，卖出时按股数比例释放
            lot.received_div_cents += gross
            portfolio.add_dividend_record(
                symbol=action.symbol,
                lot_id=lot.lot_id,
                open_date=lot.open_date,
                ex_date=action.ex_date,
                qty=lot.qty_remaining,
                gross_cents=gross,
                status="EXEMPT" if exempt else "PENDING",
                tax_rate=rate,
            )
            dividend_total += gross
        if dividend_total > 0:
            portfolio.post_cash(
                action.ex_date, dividend_total, ENTRY_DIVIDEND, 0, f"{action.symbol} 现金分红"
            )

    # 2) 送转：股数按比例增加，成本总额不变（每股成本摊薄）
    bonus = (
        book.apply_bonus_shares(action.share_ratio, ex_price, lots)
        if action.has_share_growth
        else None
    )
    qty_added = bonus.qty_added if bonus else 0
    residue_cents = bonus.residue_cents if bonus else 0
    if residue_cents > 0:
        portfolio.post_cash(
            action.ex_date, residue_cents, ENTRY_BONUS_RESIDUE, 0, f"{action.symbol} 送转碎股补偿"
        )

    # 3) 守恒校验（只针对本次在册批次）
    qty_after = sum(lot.qty_remaining for lot in lots)
    post_value, drift, allowance = check_conservation(
        action.symbol,
        pre_value,
        qty_after,
        ex_price,
        dividend_total,
        residue_cents,
    )

    return CorporateActionResult(
        symbol=action.symbol,
        ex_date=action.ex_date,
        ex_price_mills=ex_price,
        dividend_gross_cents=dividend_total,
        qty_before=qty_before,
        qty_after=qty_after,
        qty_added=qty_added,
        residue_cents=residue_cents,
        pre_value_cents=pre_value,
        post_value_cents=post_value,
        drift_cents=drift,
        allowance_cents=allowance,
    )
