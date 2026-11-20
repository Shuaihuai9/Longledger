"""差异化红利税：按 FIFO 批次的买入日判定持股期限，卖出时补扣。

A 股真实规则：
- 除息日红利全额到账（暂不扣税），税负在卖出时由中登按持股期限补扣；
- 持股期限 ≤ 1 个月（对月对日）税率 20%；
- 1 个月 < 持股期限 ≤ 1 年税率 10%；
- 超过 1 年免征；
- ETF 现金分红免征个人所得税。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from config.settings import DEFAULT_DIVIDEND_TAX_CONFIG, DividendTaxConfig
from core.fees import AssetType
from core.money import add_months, apply_rate

BRACKET_WITHIN_ONE_MONTH = "WITHIN_1M"
BRACKET_WITHIN_ONE_YEAR = "WITHIN_1Y"
BRACKET_OVER_ONE_YEAR = "OVER_1Y"
BRACKET_ETF_EXEMPT = "ETF_EXEMPT"


def holding_period_bracket(
    open_date: date,
    sell_date: date,
    asset_type: AssetType = AssetType.STOCK,
    config: DividendTaxConfig = DEFAULT_DIVIDEND_TAX_CONFIG,
) -> tuple[Decimal, str]:
    """返回 (税率, 档位标识)。持股起点取批次买入日，绝不用整体持仓日期。"""
    if sell_date < open_date:
        raise ValueError("卖出日期不能早于批次买入日期")

    if asset_type == AssetType.ETF and config.etf_exempt:
        return Decimal("0"), BRACKET_ETF_EXEMPT

    if sell_date <= add_months(open_date, 1):
        return config.rate_within_one_month, BRACKET_WITHIN_ONE_MONTH
    if sell_date <= add_months(open_date, 12):
        return config.rate_within_one_year, BRACKET_WITHIN_ONE_YEAR
    return config.rate_over_one_year, BRACKET_OVER_ONE_YEAR


def dividend_tax_on(gross_cents: int, rate: Decimal) -> int:
    """按税率计算应扣红利税（分）。"""
    if gross_cents < 0:
        raise ValueError("红利金额不能为负")
    if rate <= 0:
        return 0
    return apply_rate(gross_cents, rate)
