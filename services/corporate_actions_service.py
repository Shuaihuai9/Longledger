"""公司行为服务：自动同步分红送转 + 手动录入兜底。

职责边界：
- 取数交给 `ingest.dividends_download`，落账交给 `core` 引擎与 `LedgerService`；
  本模块只做编排，不 import 任何 UI 框架。
- 自动同步只处理「除权日 <= 同步日」的事件，并按除权日筛选在册批次：
  买入日晚于除权日的批次不参与分红/送转，避免历史事件错误补给新批次。
- 红利税逻辑完全沿用账本：除息日只登记 PENDING，卖出时按 FIFO 持股期限补扣。
- ETF 分红在第三方报表中普遍缺失，必须保留下面的手动录入 API。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core.corporate_actions import CorporateAction, CorporateActionResult
from core.money import MILLS_PER_YUAN, yuan_to_mills
from data.db import open_database
from data.repositories import Repository
from ingest.dividends_download import DividendFetchError, fetch_dividend_events
from ingest.quotes_download import parse_symbol
from services.ledger import LedgerService
from services.quotes_service import get_prev_close_mills


@dataclass
class DividendSyncResult:
    ticker: str
    fetched: int = 0
    applied: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    applied_events: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def _norm_decimal(value) -> str:
    """比例规范化：0 / 0.000 / 0E-3 都归一为 "0"，避免同一事件重复落账。"""
    return format(Decimal(str(value)).normalize(), "f")


def _action_key(ex_date: str, cash_mills: int, bonus, transfer) -> tuple:
    """统一去重键：日期 + 每股价 + 送股/转增比例。"""
    return (
        date.fromisoformat(ex_date),
        int(cash_mills),
        _norm_decimal(bonus),
        _norm_decimal(transfer),
    )


class CorporateActionService:
    """单个账户的公司行为编排。"""

    def __init__(self, conn, account_id: int = 1) -> None:
        self.conn = conn
        self.account_id = int(account_id)
        self.repo = Repository(conn, self.account_id)
        self.ledger = LedgerService(conn, self.account_id)

    # ---------- 自动同步 ----------
    def fetch_and_sync_dividends(
        self,
        ticker: str,
        *,
        timeout: float = 10,
        until: date | None = None,
    ) -> DividendSyncResult:
        """下载并落账某标的历史分红送转；单事件失败不阻断其余事件。"""
        try:
            symbol = parse_symbol(ticker).symbol
        except ValueError as exc:
            return DividendSyncResult(ticker=str(ticker), errors=[str(exc)])

        result = DividendSyncResult(ticker=symbol)
        try:
            events = fetch_dividend_events(symbol, timeout)
        except DividendFetchError as exc:
            result.errors.append(str(exc))
            return result
        result.fetched = len(events)
        if not events:
            result.note = "数据源无该标的分红记录（ETF 常见），请使用手动录入接口。"
            return result

        cutoff = until or date.today()
        existing = {
            _action_key(
                row["ex_date"],
                row["cash_per_share_mills"],
                row["bonus_ratio"],
                row["transfer_ratio"],
            )
            for row in self.repo.list_corporate_actions(symbol)
        }
        open_lots = [
            lot
            for book in self.ledger.portfolio.books.values()
            if book.symbol == symbol
            for lot in book.open_lots
        ]

        for event in events:
            if event.ex_date > cutoff:
                result.skipped += 1
                continue
            event_key = _action_key(
                event.ex_date.isoformat(),
                event.cash_per_share_mills,
                event.bonus_ratio,
                event.transfer_ratio,
            )
            if event_key in existing:
                result.skipped += 1
                continue
            if not any(lot.open_date < event.ex_date for lot in open_lots):
                # 当时没有在册持股：不落库，避免把未享受的事件标记为已处理
                result.skipped += 1
                continue

            prev_close_mills = get_prev_close_mills(
                symbol, event.ex_date, timeout=timeout
            )
            if not prev_close_mills:
                result.errors.append(
                    f"{event.ex_date.isoformat()} 缺少前收盘价，已跳过"
                )
                continue

            action = CorporateAction(
                symbol=symbol,
                ex_date=event.ex_date,
                cash_per_share_mills=event.cash_per_share_mills,
                bonus_ratio=event.bonus_ratio,
                transfer_ratio=event.transfer_ratio,
                pay_date=event.pay_date,
            )
            try:
                applied = self.ledger.apply_action(
                    action,
                    Decimal(prev_close_mills) / MILLS_PER_YUAN,
                    eligible_on=event.ex_date,
                )
            except Exception as exc:  # 单事件失败不污染其他事件
                result.errors.append(f"{event.ex_date.isoformat()} 落账失败：{exc}")
                continue

            result.applied += 1
            existing.add(event_key)
            result.applied_events.append(
                {
                    "除权日": event.ex_date.isoformat(),
                    "每股派息(元)": str(Decimal(event.cash_per_share_mills) / MILLS_PER_YUAN),
                    "派现(元)": str(Decimal(applied.dividend_gross_cents) / 100),
                    "送转股数": applied.qty_added,
                }
            )

        return result

    # ---------- 手动录入（ETF 分红等数据源缺失场景） ----------
    def record_manual_dividend(
        self,
        ticker: str,
        ex_date,
        *,
        cash_per_share_yuan="0",
        bonus_ratio="0",
        transfer_ratio="0",
        pay_date=None,
        prev_close_yuan=None,
    ) -> CorporateActionResult:
        """手动写入一条公司行为；未给前收盘价时自动取除权日前一交易日收盘。"""
        action_day = date.fromisoformat(str(ex_date)[:10])
        symbol = parse_symbol(ticker).symbol
        if prev_close_yuan is None:
            mills = get_prev_close_mills(symbol, action_day)
            if not mills:
                raise ValueError(
                    f"{symbol} 无法自动获取 {action_day.isoformat()} 的前收盘价，"
                    "请显式传入 prev_close_yuan"
                )
            prev_close_yuan = Decimal(mills) / MILLS_PER_YUAN

        action = CorporateAction(
            symbol=symbol,
            ex_date=action_day,
            cash_per_share_mills=yuan_to_mills(Decimal(str(cash_per_share_yuan))),
            bonus_ratio=Decimal(str(bonus_ratio)),
            transfer_ratio=Decimal(str(transfer_ratio)),
            pay_date=date.fromisoformat(str(pay_date)[:10]) if pay_date else action_day,
        )
        return self.ledger.apply_action(action, prev_close_yuan, eligible_on=action_day)


# ---------- 模块级便捷函数（供非 Streamlit 前端直接调用） ----------
def fetch_and_sync_dividends(
    account_id: int,
    ticker: str,
    *,
    conn=None,
    timeout: float = 10,
    until: date | None = None,
) -> DividendSyncResult:
    """自动下载并落账分红送转。

    `conn` 为空时使用默认本地数据库并在结束后关闭；传入连接则由调用方管理生命周期。
    """
    owned = conn is None
    connection = open_database() if owned else conn
    try:
        return CorporateActionService(connection, account_id).fetch_and_sync_dividends(
            ticker, timeout=timeout, until=until
        )
    finally:
        if owned:
            connection.close()


def record_manual_dividend(
    account_id: int,
    ticker: str,
    ex_date,
    *,
    cash_per_share_yuan="0",
    bonus_ratio="0",
    transfer_ratio="0",
    pay_date=None,
    prev_close_yuan=None,
    conn=None,
) -> CorporateActionResult:
    """手动录入公司行为（ETF 分红等自动数据缺失时使用）。"""
    owned = conn is None
    connection = open_database() if owned else conn
    try:
        return CorporateActionService(connection, account_id).record_manual_dividend(
            ticker,
            ex_date,
            cash_per_share_yuan=cash_per_share_yuan,
            bonus_ratio=bonus_ratio,
            transfer_ratio=transfer_ratio,
            pay_date=pay_date,
            prev_close_yuan=prev_close_yuan,
        )
    finally:
        if owned:
            connection.close()
