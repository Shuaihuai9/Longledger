"""数据仓储层：只做存取，不含任何业务规则。所有金额字段均为 INTEGER 分。

多账户：Repository 绑定单个 account_id，所有业务读写都会自动带上该账户条件。
行情 quotes 与全局费率 fee_config 不属于任何账户。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from config.settings import (
    DEFAULT_DIVIDEND_TAX_CONFIG,
    DEFAULT_FEE_CONFIG,
    LEDGER_MODE_STATEMENT,
    LEDGER_MODE_SYSTEM,
    LEDGER_MODES,
    DividendTaxConfig,
    FeeConfig,
)
from core.lots import Lot
from core.trading import (
    CashEntry,
    DividendRecord,
    Portfolio,
    TradeRecord,
)
from data.db import WRITE_LOCK

# 交割单口径不重算红利税：股息红利差异本身就是交割单里的一笔现金流水
ZERO_DIVIDEND_TAX = DividendTaxConfig(
    rate_within_one_month=Decimal("0"),
    rate_within_one_year=Decimal("0"),
    rate_over_one_year=Decimal("0"),
    etf_exempt=True,
)


def _d(text: str) -> date:
    return date.fromisoformat(text)


class Repository:
    """围绕单个 SQLite 连接、单个账户的事件存储。"""

    def __init__(self, conn: sqlite3.Connection, account_id: int = 1) -> None:
        self.conn = conn
        self.account_id = int(account_id)

    def lock(self):
        """跨线程写事务锁：调用方可用 with repo.lock() 包住一整段写入。"""
        return WRITE_LOCK

    # ---------- 交易参数（全账户共享） ----------
    def load_fee_config(self) -> FeeConfig:
        row = self.conn.execute(
            "SELECT * FROM fee_config WHERE config_id = 1"
        ).fetchone()
        if row is None:
            return DEFAULT_FEE_CONFIG
        return FeeConfig(
            commission_rate=Decimal(row["commission_rate"]),
            min_commission_cents=int(row["min_commission_cents"]),
            stamp_tax_rate=Decimal(row["stamp_tax_rate"]),
            transfer_fee_rate=Decimal(row["transfer_fee_rate"]),
            etf_stamp_tax_exempt=bool(row["etf_stamp_tax_exempt"]),
            etf_transfer_fee_exempt=bool(row["etf_transfer_fee_exempt"]),
        )

    def save_fee_config(self, config: FeeConfig) -> None:
        for label, rate in (
            ("佣金费率", config.commission_rate),
            ("印花税率", config.stamp_tax_rate),
            ("过户费率", config.transfer_fee_rate),
        ):
            if Decimal(rate) < 0 or Decimal(rate) >= 1:
                raise ValueError(f"{label}必须在 [0, 1) 区间")
        if config.min_commission_cents < 0:
            raise ValueError("最低佣金不能为负")
        with WRITE_LOCK:
            self.conn.execute(
                "INSERT OR REPLACE INTO fee_config (config_id, commission_rate,"
                " min_commission_cents, stamp_tax_rate, transfer_fee_rate,"
                " etf_stamp_tax_exempt, etf_transfer_fee_exempt, updated_at)"
                " VALUES (1,?,?,?,?,?,?,?)",
                (
                    str(config.commission_rate),
                    config.min_commission_cents,
                    str(config.stamp_tax_rate),
                    str(config.transfer_fee_rate),
                    1 if config.etf_stamp_tax_exempt else 0,
                    1 if config.etf_transfer_fee_exempt else 0,
                    date.today().isoformat(),
                ),
            )
            self.conn.commit()

    # ---------- 账户管理 ----------
    def list_accounts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM account ORDER BY account_id").fetchall()

    def get_account(self, account_id: int | None = None) -> sqlite3.Row | None:
        aid = self.account_id if account_id is None else int(account_id)
        return self.conn.execute(
            "SELECT * FROM account WHERE account_id = ?", (aid,)
        ).fetchone()

    def count_accounts(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS c FROM account").fetchone()["c"]
        )

    def account_name_exists(self, name: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM account WHERE name = ?", (name,)).fetchone()
            is not None
        )

    def create_account(
        self, name: str, initial_capital_cents: int, ledger_mode: str = "SYSTEM"
    ) -> int:
        if ledger_mode not in LEDGER_MODES:
            raise ValueError(f"未知记账口径：{ledger_mode}")
        with WRITE_LOCK:
            cursor = self.conn.execute(
                "INSERT INTO account (name, initial_capital_cents, created_at, ledger_mode)"
                " VALUES (?,?,?,?)",
                (name, initial_capital_cents, date.today().isoformat(), ledger_mode),
            )
            return int(cursor.lastrowid)


    def delete_account(self, account_id: int) -> None:
        """删除账户；关联流水由外键 ON DELETE CASCADE 一并清理。"""
        with WRITE_LOCK:
            self.conn.execute("DELETE FROM account WHERE account_id = ?", (account_id,))

    def account_cash_cents(self, account_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM cash_ledger"
            " WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row["s"])

    def account_trade_count(self, account_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row["c"])

    def account_ledger_mode(self) -> str:
        """当前账户的记账口径；老库缺列时按系统口径处理。"""
        account = self.get_account()
        if account is None or "ledger_mode" not in account.keys():
            return LEDGER_MODE_SYSTEM
        mode = str(account["ledger_mode"] or LEDGER_MODE_SYSTEM)
        return mode if mode in LEDGER_MODES else LEDGER_MODE_SYSTEM

    def is_statement_mode(self) -> bool:
        return self.account_ledger_mode() == LEDGER_MODE_STATEMENT


    # ---------- 当前账户本金 ----------
    def initial_capital_cents(self) -> int:
        row = self.get_account()
        return int(row["initial_capital_cents"]) if row else 0

    def update_initial_capital(self, cents: int) -> None:
        """改写本账户初始本金（调用方需保证口径合理，否则历史收益率会被篡改）。"""
        with WRITE_LOCK:
            self.conn.execute(
                "UPDATE account SET initial_capital_cents = ? WHERE account_id = ?",
                (cents, self.account_id),
            )

    def synthetic_initial_entry(self) -> sqlite3.Row | None:
        """首次建户自动生成的期初入金流水（entry_date = 1900-01-01）。"""
        return self.conn.execute(
            "SELECT entry_id, amount_cents FROM cash_ledger"
            " WHERE account_id = ? AND kind = 'DEPOSIT' AND entry_date = '1900-01-01'"
            " ORDER BY entry_id LIMIT 1",
            (self.account_id,),
        ).fetchone()

    def update_cash_entry_amount(self, entry_id: int, amount_cents: int) -> None:
        self.conn.execute(
            "UPDATE cash_ledger SET amount_cents = ? WHERE account_id = ? AND entry_id = ?",
            (amount_cents, self.account_id, entry_id),
        )

    def has_trades(self) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM trades WHERE account_id = ? LIMIT 1", (self.account_id,)
            ).fetchone()
            is not None
        )

    def clear_snapshots(self) -> None:
        """清空本账户历史净值快照：初始本金变更后原基准已失效。"""
        self.conn.execute(
            "DELETE FROM daily_snapshots WHERE account_id = ?", (self.account_id,)
        )

    # ---------- 交易与批次 ----------
    def save_trade(self, trade: TradeRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trades (account_id, trade_id, trade_date, symbol, side,"
            " asset_type, qty, price_mills, gross_cents, commission_cents, stamp_tax_cents,"
            " transfer_fee_cents, dividend_tax_cents, net_cash_cents, realized_pnl_cents, seq,"
            " memo, trade_logic, target_price_mills, plan_type, hold_period, sell_reason,"
            " strategy_changed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                trade.trade_id,
                trade.trade_date.isoformat(),
                trade.symbol,
                trade.side,
                trade.asset_type,
                trade.qty,
                trade.price_mills,
                trade.gross_cents,
                trade.commission_cents,
                trade.stamp_tax_cents,
                trade.transfer_fee_cents,
                trade.dividend_tax_cents,
                trade.net_cash_cents,
                trade.realized_pnl_cents,
                trade.seq,
                trade.memo,
                trade.trade_logic,
                trade.target_price_mills,
                trade.plan_type,
                trade.hold_period,
                trade.sell_reason,
                trade.strategy_changed,
            ),
        )

    def save_lots(self, symbol: str, lots: list[Lot]) -> None:
        for lot in lots:
            self.conn.execute(
                "INSERT OR REPLACE INTO trade_lots (account_id, symbol, lot_id, open_date,"
                " open_trade_id, qty_opened, qty_remaining, cost_cents, cost_remaining_cents,"
                " pending_div_cents, pending_div_shares_basis, received_div_cents)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.account_id,
                    symbol,
                    lot.lot_id,
                    lot.open_date.isoformat(),
                    lot.open_trade_id,
                    lot.qty_opened,
                    lot.qty_remaining,
                    lot.cost_cents,
                    lot.cost_remaining_cents,
                    lot.pending_div_cents,
                    lot.pending_div_shares_basis,
                    lot.received_div_cents,
                ),
            )

    def save_consumption(self, sell_trade_id: int, symbol: str, lot_id: int, item) -> None:
        self.conn.execute(
            "INSERT INTO lot_consumptions (account_id, sell_trade_id, symbol, lot_id, qty,"
            " gross_cents, fee_cents, cost_released_cents, dividend_gross_cents,"
            " dividend_tax_cents, tax_rate, tax_bracket, realized_pnl_cents)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                sell_trade_id,
                symbol,
                lot_id,
                item.qty,
                item.gross_cents,
                item.fee_cents,
                item.cost_released_cents,
                item.dividend_gross_cents,
                item.dividend_tax_cents,
                str(item.tax_rate),
                item.tax_bracket,
                item.realized_pnl_cents,
            ),
        )

    # ---------- 现金与红利 ----------
    def save_cash_entry(self, entry: CashEntry) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cash_ledger (account_id, entry_id, entry_date,"
            " amount_cents, kind, ref_id, memo, seq, qty) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                entry.entry_id,
                entry.entry_date.isoformat(),
                entry.amount_cents,
                entry.kind,
                entry.ref_id,
                entry.memo,
                entry.seq,
                entry.qty,
            ),
        )

    def update_cash_entry_qty(self, entry_id: int, qty: int) -> None:
        """回填红利流水的在册股数：旧库导入时还没有 qty 列，增量导入时顺手补齐。"""
        self.conn.execute(
            "UPDATE cash_ledger SET qty = ? WHERE account_id = ? AND entry_id = ?",
            (int(qty), self.account_id, int(entry_id)),
        )

    def save_dividend_record(self, record: DividendRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO dividend_records (account_id, record_id, symbol, lot_id,"
            " open_date, ex_date, qty, gross_cents, remaining_gross_cents, tax_rate,"
            " tax_cents, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                record.record_id,
                record.symbol,
                record.lot_id,
                record.open_date.isoformat(),
                record.ex_date.isoformat(),
                record.qty,
                record.gross_cents,
                record.remaining_gross_cents,
                str(record.tax_rate),
                record.tax_cents,
                record.status,
            ),
        )

    def save_corporate_action(
        self,
        symbol: str,
        ex_date: date,
        cash_per_share_mills: int,
        bonus_ratio: Decimal,
        transfer_ratio: Decimal,
        pay_date: date | None,
        ex_price_mills: int,
        dividend_gross_cents: int,
        qty_added: int,
        drift_cents: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO corporate_actions (account_id, symbol, ex_date,"
            " cash_per_share_mills, bonus_ratio, transfer_ratio, pay_date, ex_price_mills,"
            " dividend_gross_cents, qty_added, drift_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                symbol,
                ex_date.isoformat(),
                cash_per_share_mills,
                str(bonus_ratio),
                str(transfer_ratio),
                pay_date.isoformat() if pay_date else None,
                ex_price_mills,
                dividend_gross_cents,
                qty_added,
                drift_cents,
            ),
        )

    # ---------- 行情与快照 ----------
    def upsert_quote(self, symbol: str, price_date: date, close_mills: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO market.quotes (symbol, price_date, close_mills)"
            " VALUES (?,?,?)",
            (symbol, price_date.isoformat(), close_mills),
        )

    def prices_on(self, price_date: date) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT symbol, close_mills FROM market.quotes WHERE price_date = ?",
            (price_date.isoformat(),),
        ).fetchall()
        return {row["symbol"]: int(row["close_mills"]) for row in rows}

    def latest_price(self, symbol: str, on_or_before: date) -> int | None:
        row = self.conn.execute(
            "SELECT close_mills FROM market.quotes"
            " WHERE symbol = ? AND price_date <= ?"
            " ORDER BY price_date DESC LIMIT 1",
            (symbol, on_or_before.isoformat()),
        ).fetchone()
        return int(row["close_mills"]) if row else None

    def latest_quote_date(self, symbol: str) -> date | None:
        """该标的最新一根日线的日期；从未下载过返回 None。"""
        row = self.conn.execute(
            "SELECT price_date FROM market.quotes WHERE symbol = ?"
            " ORDER BY price_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return _d(row["price_date"]) if row else None

    def quote_coverage(self) -> list[dict]:
        """本地行情覆盖情况：按标的汇总行数、起止日期与最新收盘。"""
        rows = self.conn.execute(
            "SELECT q.symbol AS symbol, COUNT(*) AS bars,"
            " MIN(q.price_date) AS start_date, MAX(q.price_date) AS end_date,"
            " (SELECT close_mills FROM market.quotes latest"
            "  WHERE latest.symbol = q.symbol"
            "  ORDER BY latest.price_date DESC LIMIT 1) AS close_mills"
            " FROM market.quotes q GROUP BY q.symbol ORDER BY q.symbol"
        ).fetchall()
        return [
            {
                "symbol": str(row["symbol"]),
                "bars": int(row["bars"]),
                "start_date": str(row["start_date"])[:10],
                "end_date": str(row["end_date"])[:10],
                "close_mills": (
                    None if row["close_mills"] is None else int(row["close_mills"])
                ),
            }
            for row in rows
        ]

    def first_trade_date(self, symbol: str) -> date | None:
        """本账户在该标的上的首笔成交日，用于行情补全的起点兜底。"""
        row = self.conn.execute(
            "SELECT MIN(trade_date) AS day FROM trades"
            " WHERE account_id = ? AND symbol = ?",
            (self.account_id, symbol),
        ).fetchone()
        return _d(row["day"]) if row and row["day"] else None

    def save_snapshot(self, snapshot) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_snapshots (account_id, snap_date, cash_cents,"
            " market_value_cents, total_asset_cents, day_return_bp, nav_scaled,"
            " net_deposit_cents) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.account_id,
                snapshot.snap_date.isoformat(),
                snapshot.cash_cents,
                snapshot.market_value_cents,
                snapshot.total_asset_cents,
                snapshot.day_return_bp,
                snapshot.nav_scaled,
                snapshot.net_deposit_cents,
            ),
        )

    def list_snapshots(self) -> list:
        from core.valuation import DailySnapshot

        rows = self.conn.execute(
            "SELECT * FROM daily_snapshots WHERE account_id = ? ORDER BY snap_date",
            (self.account_id,),
        ).fetchall()
        return [
            DailySnapshot(
                snap_date=_d(row["snap_date"]),
                cash_cents=row["cash_cents"],
                market_value_cents=row["market_value_cents"],
                total_asset_cents=row["total_asset_cents"],
                day_return_bp=row["day_return_bp"],
                nav_scaled=row["nav_scaled"],
                net_deposit_cents=row["net_deposit_cents"],
            )
            for row in rows
        ]

    def has_cash_ledger(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM cash_ledger WHERE account_id = ? LIMIT 1", (self.account_id,)
        ).fetchone()
        return row is not None

    # ---------- 重建组合 ----------
    def load_portfolio(self) -> Portfolio:
        """从数据库重建当前账户组合：现金取流水合计，批次取当前状态。"""
        account = self.get_account()
        if account is None:
            raise ValueError(f"账户 {self.account_id} 不存在")
        initial = int(account["initial_capital_cents"])
        mode = self.account_ledger_mode()
        tax_config = (
            ZERO_DIVIDEND_TAX
            if mode == LEDGER_MODE_STATEMENT
            else DEFAULT_DIVIDEND_TAX_CONFIG
        )
        portfolio = Portfolio(
            initial_cash_cents=0,
            fee_config=self.load_fee_config(),
            tax_config=tax_config,
        )
        portfolio.initial_cash_cents = initial

        rows = self.conn.execute(
            "SELECT * FROM cash_ledger WHERE account_id = ? ORDER BY entry_id",
            (self.account_id,),
        ).fetchall()
        if rows:
            for row in rows:
                entry = CashEntry(
                    entry_id=row["entry_id"],
                    entry_date=_d(row["entry_date"]),
                    amount_cents=row["amount_cents"],
                    kind=row["kind"],
                    ref_id=row["ref_id"],
                    memo=row["memo"],
                    seq=int(row["seq"]) if "seq" in row.keys() else 0,
                    qty=int(row["qty"]) if "qty" in row.keys() else 0,
                )
                portfolio.ledger.append(entry)
                portfolio.cash_cents += entry.amount_cents
        elif initial > 0:
            # 首次使用：以初始本金作为期初余额
            portfolio.ledger.append(
                CashEntry(
                    entry_id=1,
                    entry_date=date(1900, 1, 1),
                    amount_cents=initial,
                    kind="DEPOSIT",
                    ref_id=0,
                    memo="初始入金",
                )
            )
            portfolio.cash_cents = initial

        for row in self.conn.execute(
            "SELECT * FROM trades WHERE account_id = ? ORDER BY trade_id",
            (self.account_id,),
        ):
            portfolio.trades.append(
                TradeRecord(
                    trade_id=row["trade_id"],
                    trade_date=_d(row["trade_date"]),
                    symbol=row["symbol"],
                    side=row["side"],
                    asset_type=row["asset_type"],
                    qty=row["qty"],
                    price_mills=row["price_mills"],
                    gross_cents=row["gross_cents"],
                    commission_cents=row["commission_cents"],
                    stamp_tax_cents=row["stamp_tax_cents"],
                    transfer_fee_cents=row["transfer_fee_cents"],
                    dividend_tax_cents=row["dividend_tax_cents"],
                    net_cash_cents=row["net_cash_cents"],
                    realized_pnl_cents=row["realized_pnl_cents"],
                    seq=int(row["seq"]) if "seq" in row.keys() else 0,
                    memo=row["memo"] if "memo" in row.keys() else "",
                    trade_logic=row["trade_logic"] if "trade_logic" in row.keys() else "",
                    target_price_mills=(
                        int(row["target_price_mills"])
                        if "target_price_mills" in row.keys()
                        else 0
                    ),
                    plan_type=row["plan_type"] if "plan_type" in row.keys() else "",
                    hold_period=(
                        row["hold_period"] if "hold_period" in row.keys() else ""
                    ),
                    sell_reason=(
                        row["sell_reason"] if "sell_reason" in row.keys() else ""
                    ),
                    strategy_changed=(
                        row["strategy_changed"]
                        if "strategy_changed" in row.keys()
                        else ""
                    ),
                )
            )

        for row in self.conn.execute(
            "SELECT l.*, COALESCE(t.price_mills, 0) AS buy_price_mills"
            " FROM trade_lots l"
            " LEFT JOIN trades t"
            "   ON t.account_id = l.account_id AND t.trade_id = l.open_trade_id"
            " WHERE l.account_id = ? ORDER BY l.symbol, l.lot_id",
            (self.account_id,),
        ):
            book = portfolio.book(row["symbol"])
            book.lots.append(
                Lot(
                    lot_id=row["lot_id"],
                    symbol=row["symbol"],
                    open_date=_d(row["open_date"]),
                    qty_opened=row["qty_opened"],
                    qty_remaining=row["qty_remaining"],
                    cost_cents=row["cost_cents"],
                    cost_remaining_cents=row["cost_remaining_cents"],
                    buy_price_mills=int(row["buy_price_mills"]),
                    pending_div_cents=row["pending_div_cents"],
                    pending_div_shares_basis=row["pending_div_shares_basis"],
                    received_div_cents=(
                        row["received_div_cents"] if "received_div_cents" in row.keys() else 0
                    ),
                    open_trade_id=row["open_trade_id"],
                )
            )
            book._next_lot_id = max(book._next_lot_id, int(row["lot_id"]) + 1)

        for row in self.conn.execute(
            "SELECT * FROM dividend_records WHERE account_id = ? ORDER BY record_id",
            (self.account_id,),
        ):
            portfolio.dividends.append(
                DividendRecord(
                    record_id=row["record_id"],
                    symbol=row["symbol"],
                    lot_id=row["lot_id"],
                    open_date=_d(row["open_date"]),
                    ex_date=_d(row["ex_date"]),
                    qty=row["qty"],
                    gross_cents=row["gross_cents"],
                    remaining_gross_cents=row["remaining_gross_cents"],
                    tax_rate=Decimal(row["tax_rate"]),
                    tax_cents=row["tax_cents"],
                    status=row["status"],
                )
            )

        if portfolio.trades:
            portfolio._next_trade_id = max(t.trade_id for t in portfolio.trades) + 1
        if portfolio.ledger:
            portfolio._next_entry_id = max(e.entry_id for e in portfolio.ledger) + 1
        if portfolio.dividends:
            portfolio._next_dividend_id = max(r.record_id for r in portfolio.dividends) + 1
        return portfolio

    # ---------- 查询视图 ----------
    def positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trade_lots WHERE account_id = ? AND qty_remaining > 0"
            " ORDER BY symbol, open_date",
            (self.account_id,),
        ).fetchall()

    def list_timed_consumptions(self, symbol: str) -> list[sqlite3.Row]:
        """某标的的 FIFO 消耗链，带卖出日期：用于还原历史任意时点的在册股数。"""
        return self.conn.execute(
            "SELECT c.lot_id, c.qty, c.sell_trade_id, t.trade_date AS sell_date"
            " FROM lot_consumptions c"
            " JOIN trades t ON t.account_id = c.account_id AND t.trade_id = c.sell_trade_id"
            " WHERE c.account_id = ? AND c.symbol = ?"
            " ORDER BY t.trade_date, c.consumption_id",
            (self.account_id, symbol),
        ).fetchall()

    def list_corporate_actions(self, symbol: str | None = None) -> list[sqlite3.Row]:
        """本账户已落库的公司行为；可按标的过滤。"""
        if symbol is None:
            return self.conn.execute(
                "SELECT * FROM corporate_actions WHERE account_id = ?"
                " ORDER BY ex_date, action_id",
                (self.account_id,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM corporate_actions WHERE account_id = ? AND symbol = ?"
            " ORDER BY ex_date, action_id",
            (self.account_id, symbol),
        ).fetchall()

    def dividend_records_for_lot(self, symbol: str, lot_id: int) -> list[sqlite3.Row]:
        """某批次逐条红利记录（含已扣税与待缴），用于批次明细展示。"""
        return self.conn.execute(
            "SELECT * FROM dividend_records WHERE account_id = ? AND symbol = ? AND lot_id = ?"
            " ORDER BY ex_date, record_id",
            (self.account_id, symbol, lot_id),
        ).fetchall()

    def dividends_pending(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM dividend_records WHERE account_id = ? AND status = 'PENDING'"
            " ORDER BY ex_date, symbol",
            (self.account_id,),
        ).fetchall()

    def list_trades(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE account_id = ?"
            " ORDER BY trade_date DESC, trade_id DESC LIMIT ?",
            (self.account_id, limit),
        ).fetchall()

    def list_cash_entries(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cash_ledger WHERE account_id = ? ORDER BY entry_id",
            (self.account_id,),
        ).fetchall()

    def wipe_ledger_data(self) -> None:
        """清空本账户全部派生业务数据，供删改成交后的全量重放使用。

        调用方必须在同一事务内完成重建并 commit，否则账户数据会被掏空。
        """
        for table in (
            "lot_consumptions",
            "dividend_records",
            "trade_lots",
            "trades",
            "corporate_actions",
            "cash_ledger",
            "daily_snapshots",
        ):
            self.conn.execute(
                f"DELETE FROM {table} WHERE account_id = ?", (self.account_id,)
            )

    def list_ledger(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cash_ledger WHERE account_id = ?"
            " ORDER BY entry_id DESC LIMIT ?",
            (self.account_id, limit),
        ).fetchall()

    def commit(self) -> None:
        with WRITE_LOCK:
            self.conn.commit()

    def rollback(self) -> None:
        with WRITE_LOCK:
            self.conn.rollback()
