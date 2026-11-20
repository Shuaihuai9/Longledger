"""金额与日期基础工具。

精度契约（全项目唯一口径）：
- 资金：INTEGER，单位「分」（cent）。绝不使用 float。
- 价格：INTEGER，单位「毫」（mill，1 元 = 1000 毫），兼容 ETF 三位小数报价。
- 数量：INTEGER，单位「股」。
- 一切乘除均经 Decimal + 指定舍入，中间结果绝不落 float。
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

MILLS_PER_CENT = 10
CENTS_PER_YUAN = 100
MILLS_PER_YUAN = 1000

__all__ = [
    "MILLS_PER_CENT",
    "CENTS_PER_YUAN",
    "MILLS_PER_YUAN",
    "round_half_up",
    "floor_int",
    "yuan_to_cents",
    "yuan_to_mills",
    "cents_to_yuan",
    "mills_to_yuan",
    "format_cents",
    "format_mills",
    "qty_price_to_cents",
    "apply_rate",
    "prorate",
    "add_months",
]


def round_half_up(value: Decimal | int | str) -> int:
    """四舍五入到整数（分）。使用 Decimal，避免浮点误差。"""
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def floor_int(value: Decimal | int | str) -> int:
    """向下取整，用于送转股的碎股计算。"""
    return int(Decimal(value).to_integral_value(rounding=ROUND_DOWN))


def yuan_to_cents(yuan: Decimal | int | str | float) -> int:
    """人民币元 -> 分。float 入参会先转字符串再进 Decimal，仅用于外部输入。"""
    return round_half_up(Decimal(str(yuan)) * CENTS_PER_YUAN)


def yuan_to_mills(yuan: Decimal | int | str) -> int:
    """人民币元 -> 毫（价格精度）。"""
    return round_half_up(Decimal(str(yuan)) * MILLS_PER_YUAN)


def cents_to_yuan(cents: int) -> Decimal:
    return Decimal(cents) / CENTS_PER_YUAN


def mills_to_yuan(mills: int) -> Decimal:
    return Decimal(mills) / MILLS_PER_YUAN


def format_cents(cents: int, with_sign: bool = False) -> str:
    """分 -> 带千分位的元字符串，展示层专用。"""
    text = f"{Decimal(abs(cents)) / CENTS_PER_YUAN:,.2f}"
    if cents < 0:
        return "-" + text
    if with_sign and cents > 0:
        return "+" + text
    return text


def format_mills(mills: int) -> str:
    """毫 -> 三位小数的元字符串。"""
    return f"{Decimal(mills) / MILLS_PER_YUAN:.3f}"


def qty_price_to_cents(qty: int, price_mills: int) -> int:
    """成交金额：股 × 毫/股 ÷ 10 = 分。"""
    if qty < 0 or price_mills < 0:
        raise ValueError("数量与价格不能为负")
    return round_half_up(Decimal(qty) * Decimal(price_mills) / MILLS_PER_CENT)


def apply_rate(cents: int, rate: Decimal) -> int:
    """按费率计算费用金额（分），四舍五入。"""
    if cents < 0:
        raise ValueError("计费基数不能为负")
    return round_half_up(Decimal(cents) * Decimal(rate))


def prorate(total: int, part: int, whole: int) -> int:
    """按比例分摊，最后一批由调用方取余额以保证总额精确闭合。"""
    if whole <= 0:
        raise ValueError("分摊基数必须为正")
    if part < 0 or part > whole:
        raise ValueError("分摊份数越界")
    if part == whole:
        return total
    return round_half_up(Decimal(total) * Decimal(part) / Decimal(whole))


def add_months(day: date, months: int) -> date:
    """对月对日推进日期（月末自动收敛），用于持股期限的档位边界判定。"""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))
