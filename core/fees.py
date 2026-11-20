"""交易摩擦成本引擎：佣金（含最低 5 元保底）、印花税（仅卖出单边）、过户费。

所有交易必须经此唯一出口，禁止在别处另算费用。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from config.settings import DEFAULT_FEE_CONFIG, FeeConfig
from core.money import apply_rate, qty_price_to_cents

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


class AssetType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"


# 场内基金代码段：沪市 50/51/52/56/58 开头，深市 15/16 开头（含 159 ETF、16x LOF）
_ETF_PREFIXES = ("50", "51", "52", "56", "58", "15", "16")


def classify_symbol(symbol: str) -> AssetType:
    """按代码段自动识别 ETF；无法识别时保守按股票处理。"""
    code = str(symbol).split(".")[0].strip()
    if len(code) == 6 and code.isdigit() and code.startswith(_ETF_PREFIXES):
        return AssetType.ETF
    return AssetType.STOCK


@dataclass(frozen=True)
class FeeBreakdown:
    """一笔交易的费用明细（分）。"""

    commission_cents: int = 0
    stamp_tax_cents: int = 0
    transfer_fee_cents: int = 0

    @property
    def total_cents(self) -> int:
        return self.commission_cents + self.stamp_tax_cents + self.transfer_fee_cents

    def as_dict(self) -> dict[str, int]:
        return {
            "commission_cents": self.commission_cents,
            "stamp_tax_cents": self.stamp_tax_cents,
            "transfer_fee_cents": self.transfer_fee_cents,
            "total_cents": self.total_cents,
        }


def calculate_fees(
    side: str,
    qty: int,
    price_mills: int,
    symbol: str,
    config: FeeConfig = DEFAULT_FEE_CONFIG,
    asset_type: AssetType | None = None,
) -> FeeBreakdown:
    """计算一笔成交的佣金、印花税、过户费。

    规则：
    - 佣金双边收取，且 `max(成交额 × 费率, 最低 5 元)` —— 硬编码保底，不可绕过。
    - 印花税仅卖出单边，ETF 免征。
    - 过户费双边，ETF 免征（可配置）。
    """
    if qty <= 0:
        raise ValueError("成交数量必须为正整数")
    if price_mills <= 0:
        raise ValueError("成交价格必须为正")
    if side not in (SIDE_BUY, SIDE_SELL):
        raise ValueError(f"未知交易方向: {side}")

    amount_cents = qty_price_to_cents(qty, price_mills)
    is_etf = (asset_type or classify_symbol(symbol)) == AssetType.ETF

    commission = apply_rate(amount_cents, config.commission_rate)
    if commission < config.min_commission_cents:
        commission = config.min_commission_cents

    stamp_tax = 0
    if side == SIDE_SELL and not (is_etf and config.etf_stamp_tax_exempt):
        stamp_tax = apply_rate(amount_cents, config.stamp_tax_rate)

    transfer_fee = 0
    if not (is_etf and config.etf_transfer_fee_exempt):
        transfer_fee = apply_rate(amount_cents, config.transfer_fee_rate)

    return FeeBreakdown(
        commission_cents=commission,
        stamp_tax_cents=stamp_tax,
        transfer_fee_cents=transfer_fee,
    )


def total_cost_of_buy(qty: int, price_mills: int, fees: FeeBreakdown) -> int:
    """买入总成本（分）= 成交额 + 全部费用。计入批次成本。"""
    return qty_price_to_cents(qty, price_mills) + fees.total_cents
