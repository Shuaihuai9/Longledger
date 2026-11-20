"""每日结算：按行情快照生成净值与收益率。"""
from __future__ import annotations

from datetime import date

from core.valuation import DailySnapshot, build_snapshot
from data.repositories import Repository


def settle_day(repo: Repository, portfolio, snap_date: date) -> DailySnapshot:
    """结算某日净值；行情缺失的标的按成本估值，避免市值凭空归零。"""
    prices: dict[str, int] = {}
    for symbol, book in portfolio.books.items():
        if book.total_qty == 0:
            continue
        price = repo.latest_price(symbol, snap_date)
        if price is not None:
            prices[symbol] = price

    snapshots = repo.list_snapshots()
    prev_total = (
        snapshots[-1].total_asset_cents if snapshots else portfolio.initial_cash_cents
    )
    net_deposit = sum(
        entry.amount_cents
        for entry in portfolio.ledger
        if entry.kind in ("DEPOSIT", "WITHDRAW") and entry.entry_date == snap_date
    )
    # 交割单口径期初现金通常为 0，净值基准改用入金 / 出金流水累计的投入本金
    capital_base = portfolio.initial_cash_cents or sum(
        entry.amount_cents
        for entry in portfolio.ledger
        if entry.kind in ("DEPOSIT", "WITHDRAW")
    )

    snapshot = build_snapshot(
        snap_date=snap_date,
        cash_cents=portfolio.cash_cents,
        market_value_cents=portfolio.market_value_cents(prices),
        prev_total_asset_cents=prev_total,
        initial_capital_cents=capital_base,
        net_deposit_cents=net_deposit,
    )
    repo.save_snapshot(snapshot)
    repo.commit()
    return snapshot
