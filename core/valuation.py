"""每日净值、收益率、年化与最大回撤。全链路整数分，净值用定点整数存储。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from config.settings import NAV_SCALE
from core.money import round_half_up


@dataclass(frozen=True)
class DailySnapshot:
    snap_date: date
    cash_cents: int
    market_value_cents: int
    total_asset_cents: int
    day_return_bp: int        # 当日收益率，单位「基点」(1bp = 0.01%)
    nav_scaled: int           # 净值 × NAV_SCALE 的整数表示
    net_deposit_cents: int    # 当日净入金（用于剔除出入金对收益率的干扰）


def nav_scaled(total_asset_cents: int, initial_capital_cents: int) -> int:
    if initial_capital_cents <= 0:
        raise ValueError("初始本金必须为正")
    return round_half_up(
        Decimal(total_asset_cents) * NAV_SCALE / Decimal(initial_capital_cents)
    )


def day_return_bp(
    total_asset_cents: int,
    prev_total_asset_cents: int,
    net_deposit_cents: int = 0,
) -> int:
    """当日收益率（基点）= (今总资产 - 净入金 - 昨总资产) / 昨总资产。"""
    if prev_total_asset_cents <= 0:
        return 0
    base = prev_total_asset_cents
    pnl = total_asset_cents - net_deposit_cents - base
    return round_half_up(Decimal(pnl) * 10_000 / Decimal(base))


def build_snapshot(
    snap_date: date,
    cash_cents: int,
    market_value_cents: int,
    prev_total_asset_cents: int,
    initial_capital_cents: int,
    net_deposit_cents: int = 0,
) -> DailySnapshot:
    total = cash_cents + market_value_cents
    return DailySnapshot(
        snap_date=snap_date,
        cash_cents=cash_cents,
        market_value_cents=market_value_cents,
        total_asset_cents=total,
        day_return_bp=day_return_bp(total, prev_total_asset_cents, net_deposit_cents),
        nav_scaled=nav_scaled(total, initial_capital_cents),
        net_deposit_cents=net_deposit_cents,
    )


def annualized_return_bp(snapshots: list[DailySnapshot]) -> int:
    """按累计净值折算年化收益率（基点）。不足 1 个自然日返回 0。"""
    if len(snapshots) < 2:
        return 0
    first, last = snapshots[0], snapshots[-1]
    days = (last.snap_date - first.snap_date).days
    if days <= 0 or first.nav_scaled <= 0:
        return 0
    growth = Decimal(last.nav_scaled) / Decimal(first.nav_scaled)
    if growth <= 0:
        return 0
    # 年化 = growth^(365/days) - 1，用 Decimal 幂运算保持精度
    annual = growth ** (Decimal(365) / Decimal(days)) - Decimal("1")
    return round_half_up(annual * 10_000)


def max_drawdown_bp(snapshots: list[DailySnapshot]) -> int:
    """最大回撤（基点），按净值序列峰谷计算。"""
    peak = None
    worst = 0
    for snap in snapshots:
        if peak is None or snap.nav_scaled > peak:
            peak = snap.nav_scaled
        if peak and peak > 0:
            drawdown = round_half_up(Decimal(peak - snap.nav_scaled) * 10_000 / Decimal(peak))
            worst = max(worst, drawdown)
    return worst
