"""数据库连接、初始化与 v2 -> v3 多账户迁移。

线程模型：Streamlit 每次脚本重跑都在新线程执行，因此连接必须允许跨线程复用
（check_same_thread=False）；同时写操作由全局可重入锁串行化，避免并发重跑互相
覆盖事务。读操作不加锁，SQLite 自身保证读一致性。

迁移策略：v2 的所有业务表没有 account_id 且主键是单列，无法用 ALTER 加列，
因此采用「重命名旧表 -> 建新表 -> 复制数据（account_id 统一记为 1）-> 删旧表」。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path

from config.settings import (
    DB_PATH,
    DEFAULT_FEE_CONFIG,
    DEFAULT_INITIAL_CAPITAL_CENTS,
    QUOTES_DB_PATH,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "11"

# 行情库（market.db）只放 quotes 表，与账户库通过 ATTACH 合并到一个连接里，
# 这样所有既有 SQL 仍然在一个事务内可见，而两个文件可以放在不同目录。
_QUOTES_DDL = (
    "CREATE TABLE IF NOT EXISTS quotes ("
    " symbol TEXT NOT NULL,"
    " price_date TEXT NOT NULL,"
    " close_mills INTEGER NOT NULL CHECK (close_mills > 0),"
    " PRIMARY KEY (symbol, price_date))"
)

# 所有写事务共用一把可重入锁
WRITE_LOCK = threading.RLock()

_OLD_TABLES = (
    "lot_consumptions",
    "dividend_records",
    "trade_lots",
    "trades",
    "cash_ledger",
    "corporate_actions",
    "daily_snapshots",
    "account",
)

_OLD_INDEXES = (
    "idx_trades_symbol_date",
    "idx_lots_fifo",
    "idx_consumptions_trade",
    "idx_dividends_pending",
    "idx_ledger_date",
)

# 表名 -> (新表列, 从旧表 SELECT 的表达式)。旧库所有流水默认归属账户 1。
_COPY_COLUMNS: dict[str, tuple[str, str]] = {
    "trades": (
        "account_id, trade_id, trade_date, symbol, side, asset_type, qty, price_mills,"
        " gross_cents, commission_cents, stamp_tax_cents, transfer_fee_cents,"
        " dividend_tax_cents, net_cash_cents, realized_pnl_cents",
        "1, trade_id, trade_date, symbol, side, asset_type, qty, price_mills,"
        " gross_cents, commission_cents, stamp_tax_cents, transfer_fee_cents,"
        " dividend_tax_cents, net_cash_cents, realized_pnl_cents",
    ),
    "trade_lots": (
        "account_id, symbol, lot_id, open_date, open_trade_id, qty_opened, qty_remaining,"
        " cost_cents, cost_remaining_cents, pending_div_cents, pending_div_shares_basis",
        "1, symbol, lot_id, open_date, open_trade_id, qty_opened, qty_remaining,"
        " cost_cents, cost_remaining_cents, pending_div_cents, pending_div_shares_basis",
    ),
    "lot_consumptions": (
        "consumption_id, account_id, sell_trade_id, symbol, lot_id, qty, gross_cents,"
        " fee_cents, cost_released_cents, dividend_gross_cents, dividend_tax_cents,"
        " tax_rate, tax_bracket, realized_pnl_cents",
        "consumption_id, 1, sell_trade_id, symbol, lot_id, qty, gross_cents,"
        " fee_cents, cost_released_cents, dividend_gross_cents, dividend_tax_cents,"
        " tax_rate, tax_bracket, realized_pnl_cents",
    ),
    "corporate_actions": (
        "action_id, account_id, symbol, ex_date, cash_per_share_mills, bonus_ratio,"
        " transfer_ratio, pay_date, ex_price_mills, dividend_gross_cents, qty_added,"
        " drift_cents",
        "action_id, 1, symbol, ex_date, cash_per_share_mills, bonus_ratio,"
        " transfer_ratio, pay_date, ex_price_mills, dividend_gross_cents, qty_added,"
        " drift_cents",
    ),
    "dividend_records": (
        "account_id, record_id, symbol, lot_id, open_date, ex_date, qty, gross_cents,"
        " remaining_gross_cents, tax_rate, tax_cents, status",
        "1, record_id, symbol, lot_id, open_date, ex_date, qty, gross_cents,"
        " remaining_gross_cents, tax_rate, tax_cents, status",
    ),
    "cash_ledger": (
        "account_id, entry_id, entry_date, amount_cents, kind, ref_id, memo",
        "1, entry_id, entry_date, amount_cents, kind, ref_id, memo",
    ),
    "daily_snapshots": (
        "account_id, snap_date, cash_cents, market_value_cents, total_asset_cents,"
        " day_return_bp, nav_scaled, net_deposit_cents",
        "1, snap_date, cash_cents, market_value_cents, total_asset_cents,"
        " day_return_bp, nav_scaled, net_deposit_cents",
    ),
}


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """建立连接：允许跨线程使用、WAL 模式、开启外键。"""
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _needs_v3(conn: sqlite3.Connection) -> bool:
    """v2 及更早：trades 表存在但没有 account_id 列。"""
    if "trades" not in _table_names(conn):
        return False
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    return "account_id" not in columns


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """把单账户旧库原地升级为多账户结构，所有历史数据归属 account_id = 1。"""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        "".join(f"DROP INDEX IF EXISTS {name};" for name in _OLD_INDEXES)
    )
    for table in _OLD_TABLES:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")

    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    conn.execute(
        "INSERT INTO account (account_id, name, initial_capital_cents, created_at)"
        " SELECT account_id, name, initial_capital_cents, created_at FROM account_v2"
    )
    for table, (new_columns, old_select) in _COPY_COLUMNS.items():
        conn.execute(
            f"INSERT INTO {table} ({new_columns}) SELECT {old_select} FROM {table}_v2"
        )
    for table in _OLD_TABLES:
        conn.execute(f"DROP TABLE {table}_v2")

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _ensure_account_mode(conn: sqlite3.Connection) -> None:
    """v4：老库补上 ledger_mode 列；已有账户一律沿用「系统计算」口径。"""
    if "account" not in _table_names(conn):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(account)").fetchall()}
    if "ledger_mode" not in columns:
        # SQLite 的 ADD COLUMN 不能带 CHECK，约束只写在新库的建表语句里
        conn.execute(
            "ALTER TABLE account ADD COLUMN ledger_mode TEXT NOT NULL DEFAULT 'SYSTEM'"
        )


def _ensure_event_seq(conn: sqlite3.Connection) -> None:
    """v5：成交与现金流水补上 seq 列，用于交割单口径恢复同日原始行序。"""
    for table in ("trades", "cash_ledger"):
        if table not in _table_names(conn):
            continue
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "seq" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")


def _rebalance_repo_entries(conn: sqlite3.Connection) -> None:
    """v6：逆回购本金不计现金，旧 REPO 流水按同备注配对拆成利息后删除。

    老版本把逆回购开仓当成出金，导致「可用现金」比券商少一笔未到期本金。
    这里把未结本金留在现金里，只把到期差额（利息）补成收益流水。
    """
    if "cash_ledger" not in _table_names(conn):
        return
    rows = conn.execute(
        "SELECT account_id, entry_id, entry_date, amount_cents, memo, seq FROM cash_ledger"
        " WHERE kind = 'REPO' ORDER BY account_id, entry_date, entry_id, seq"
    ).fetchall()
    if not rows:
        return

    by_account: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_account.setdefault(int(row["account_id"]), []).append(row)

    for account_id, items in by_account.items():
        open_principal: dict[str, list[int]] = {}
        interests: list[tuple[str, int, str, int]] = []
        for row in items:
            memo = row["memo"] or ""
            amount = int(row["amount_cents"])
            if amount < 0:
                open_principal.setdefault(memo, []).append(-amount)
                continue
            queue = open_principal.get(memo, [])
            match = None
            best_delta = None
            for principal in queue:
                delta = amount - principal
                if 0 <= delta <= 1000 and (best_delta is None or delta < best_delta):
                    match, best_delta = principal, delta
            if match is None:
                continue
            queue.remove(match)
            interest = amount - match
            if interest > 0:
                interests.append(
                    (row["entry_date"], interest, f"{memo} 回购利息", int(row["seq"]))
                )

        conn.execute(
            "DELETE FROM cash_ledger WHERE account_id = ? AND kind = 'REPO'",
            (account_id,),
        )
        next_id = int(
            conn.execute(
                "SELECT COALESCE(MAX(entry_id), 0) AS m FROM cash_ledger WHERE account_id = ?",
                (account_id,),
            ).fetchone()["m"]
        ) + 1
        for entry_date, amount, memo, seq in interests:
            conn.execute(
                "INSERT INTO cash_ledger (account_id, entry_id, entry_date, amount_cents,"
                " kind, ref_id, memo, seq) VALUES (?,?,?,?,?,?,?,?)",
                (account_id, next_id, entry_date, amount, "STATEMENT_INTEREST", 0, memo, seq),
            )
            next_id += 1


_REBUILD_TRADES_V7 = """
BEGIN;
CREATE TABLE trades_v7 (
    account_id          INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    trade_id            INTEGER NOT NULL,
    trade_date          TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    asset_type          TEXT    NOT NULL CHECK (asset_type IN ('STOCK', 'ETF')),
    qty                 INTEGER NOT NULL CHECK (qty > 0),
    price_mills         INTEGER NOT NULL CHECK (price_mills >= 0),
    gross_cents         INTEGER NOT NULL CHECK (gross_cents >= 0),
    commission_cents    INTEGER NOT NULL CHECK (commission_cents >= 0),
    stamp_tax_cents     INTEGER NOT NULL CHECK (stamp_tax_cents >= 0),
    transfer_fee_cents  INTEGER NOT NULL CHECK (transfer_fee_cents >= 0),
    dividend_tax_cents  INTEGER NOT NULL DEFAULT 0 CHECK (dividend_tax_cents >= 0),
    net_cash_cents      INTEGER NOT NULL,
    realized_pnl_cents  INTEGER NOT NULL DEFAULT 0,
    seq                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, trade_id)
);
INSERT INTO trades_v7 SELECT * FROM trades;
DROP TABLE trades;
ALTER TABLE trades_v7 RENAME TO trades;
CREATE INDEX IF NOT EXISTS idx_trades_symbol_date ON trades (account_id, symbol, trade_date);
COMMIT;
"""


def _ensure_zero_price_trades(conn: sqlite3.Connection) -> None:
    """v7：允许 0 价成交，用于交割单口径下红股/送转/新股入账的 0 成本批次。

    SQLite 不能直接修改 CHECK 约束，只能重建 trades 表；重建期间临时关闭外键
    校验，trade_lots / lot_consumptions 的外键仍指向重建后的 `trades`。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trades'"
    ).fetchone()
    if row is None or "price_mills > 0" not in (row["sql"] or ""):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(_REBUILD_TRADES_V7)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _ensure_lot_received_div(conn: sqlite3.Connection) -> None:
    """v8：批次补上累计已收现金分红列（原始成本/摊薄成本双口径展示）。"""
    if "trade_lots" not in _table_names(conn):
        return
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(trade_lots)").fetchall()
    }
    if "received_div_cents" not in columns:
        conn.execute(
            "ALTER TABLE trade_lots ADD COLUMN received_div_cents INTEGER NOT NULL DEFAULT 0"
        )


def _ensure_trade_memo(conn: sqlite3.Connection) -> None:
    """v9：成交补上原始摘要列，0 成本红股/送转据此区分送股与转增。"""
    if "trades" not in _table_names(conn):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "memo" not in columns:
        conn.execute("ALTER TABLE trades ADD COLUMN memo TEXT NOT NULL DEFAULT ''")


def _ensure_ledger_qty(conn: sqlite3.Connection) -> None:
    """v10：现金流水补上成交数量，红利入账按交割单给出的在册股数拆分批次。"""
    if "cash_ledger" not in _table_names(conn):
        return
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cash_ledger)").fetchall()
    }
    if "qty" not in columns:
        conn.execute("ALTER TABLE cash_ledger ADD COLUMN qty INTEGER NOT NULL DEFAULT 0")


def _ensure_trade_notes(conn: sqlite3.Connection) -> None:
    """v11：成交补上交易复盘字段（买卖逻辑 / 目标价 / 计划 / 周期 / 卖出原因 / 策略变更）。"""
    if "trades" not in _table_names(conn):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    additions = (
        ("trade_logic", "TEXT NOT NULL DEFAULT ''"),
        ("target_price_mills", "INTEGER NOT NULL DEFAULT 0"),
        ("plan_type", "TEXT NOT NULL DEFAULT ''"),
        ("hold_period", "TEXT NOT NULL DEFAULT ''"),
        ("sell_reason", "TEXT NOT NULL DEFAULT ''"),
        ("strategy_changed", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, ddl in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")


def init_db(conn: sqlite3.Connection, initial_capital_cents: int = DEFAULT_INITIAL_CAPITAL_CENTS) -> None:
    """建表/迁移，并在账户为空时写入默认账户与默认费率（幂等）。"""
    with WRITE_LOCK:
        if _needs_v3(conn):
            _migrate_v2_to_v3(conn)

        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _ensure_account_mode(conn)
        _ensure_event_seq(conn)
        _rebalance_repo_entries(conn)
        _ensure_zero_price_trades(conn)
        _ensure_lot_received_div(conn)
        _ensure_trade_memo(conn)
        _ensure_ledger_qty(conn)
        _ensure_trade_notes(conn)

        has_account = conn.execute("SELECT COUNT(*) AS c FROM account").fetchone()["c"] > 0
        if not has_account:
            conn.execute(
                "INSERT INTO account (account_id, name, initial_capital_cents, created_at)"
                " VALUES (1, ?, ?, ?)",
                ("长衡主账户", initial_capital_cents, date.today().isoformat()),
            )
        conn.execute(
            "INSERT OR IGNORE INTO fee_config (config_id, commission_rate, min_commission_cents,"
            " stamp_tax_rate, transfer_fee_rate, etf_stamp_tax_exempt, etf_transfer_fee_exempt,"
            " updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(DEFAULT_FEE_CONFIG.commission_rate),
                DEFAULT_FEE_CONFIG.min_commission_cents,
                str(DEFAULT_FEE_CONFIG.stamp_tax_rate),
                str(DEFAULT_FEE_CONFIG.transfer_fee_rate),
                1 if DEFAULT_FEE_CONFIG.etf_stamp_tax_exempt else 0,
                1 if DEFAULT_FEE_CONFIG.etf_transfer_fee_exempt else 0,
                date.today().isoformat(),
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()


def _attach_quotes(conn: sqlite3.Connection, quotes_path: Path) -> None:
    """挂载行情库；首次挂载时建表，并把旧版 main.quotes 里的数据搬过去。"""
    conn.execute("ATTACH DATABASE ? AS market", (str(quotes_path),))
    try:
        conn.execute("PRAGMA market.journal_mode = WAL")
    except sqlite3.Error:
        pass
    conn.execute(_QUOTES_DDL.replace("quotes", "market.quotes", 1))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS market.idx_quotes_date ON quotes (price_date)"
    )
    count = int(
        conn.execute("SELECT COUNT(*) AS c FROM market.quotes").fetchone()["c"]
    )
    if count == 0:
        conn.execute(
            "INSERT OR IGNORE INTO market.quotes (symbol, price_date, close_mills)"
            " SELECT symbol, price_date, close_mills FROM main.quotes"
        )
    conn.commit()


def open_database(
    db_path: Path | str | None = None,
    quotes_path: Path | str | None = None,
) -> sqlite3.Connection:
    """打开账户库并挂载行情库。

    默认路径来自 config.settings：安装版为 %APPDATA%\\LongLedger\\data\\longledger.db
    与 %LOCALAPPDATA%\\LongLedger\\market\\market.db（可由安装器改写 paths.ini）；
    显式传入 db_path 时（测试/工具），行情库默认放在同目录的 market.db。
    """
    if db_path is None:
        db_path = DB_PATH
        if quotes_path is None:
            quotes_path = QUOTES_DB_PATH
    db_path = Path(db_path)
    if quotes_path is None:
        quotes_path = db_path.parent / "market.db"
    quotes_path = Path(quotes_path)
    if quotes_path.resolve() == db_path.resolve():
        quotes_path = db_path.with_name("market.db")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    quotes_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    _attach_quotes(conn, quotes_path)
    return conn
