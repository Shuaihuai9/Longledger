-- 长衡 LongLedger 数据库结构（schema v3：多账户）
-- 精度契约：所有 *_cents / *_mills / qty 字段一律 INTEGER，禁止 REAL。
-- 账户隔离：除行情 quotes 与全局费率 fee_config 外，所有业务表均带 account_id。
-- 注意：连接级 PRAGMA foreign_keys 由 data/db.py 统一设置，此处不重复声明。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 账户：每个账户有独立的现金、批次、流水与净值快照
CREATE TABLE IF NOT EXISTS account (
    account_id            INTEGER PRIMARY KEY,
    name                  TEXT    NOT NULL,
    initial_capital_cents INTEGER NOT NULL CHECK (initial_capital_cents >= 0),
    created_at            TEXT    NOT NULL,
    -- SYSTEM：费用/红利由费率引擎计算；STATEMENT：一律按导入交割单的真实金额入账
    ledger_mode           TEXT    NOT NULL DEFAULT 'SYSTEM'
);

CREATE TABLE IF NOT EXISTS trades (
    account_id          INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    trade_id            INTEGER NOT NULL,
    trade_date          TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    asset_type          TEXT    NOT NULL CHECK (asset_type IN ('STOCK', 'ETF')),
    qty                 INTEGER NOT NULL CHECK (qty > 0),
    -- 0 价成交只用于红股/送转/新股入账的 0 成本批次，普通成交价格必须为正
    price_mills         INTEGER NOT NULL CHECK (price_mills >= 0),
    gross_cents         INTEGER NOT NULL CHECK (gross_cents >= 0),
    commission_cents    INTEGER NOT NULL CHECK (commission_cents >= 0),
    stamp_tax_cents     INTEGER NOT NULL CHECK (stamp_tax_cents >= 0),
    transfer_fee_cents  INTEGER NOT NULL CHECK (transfer_fee_cents >= 0),
    dividend_tax_cents  INTEGER NOT NULL DEFAULT 0 CHECK (dividend_tax_cents >= 0),
    net_cash_cents      INTEGER NOT NULL,
    realized_pnl_cents  INTEGER NOT NULL DEFAULT 0,
    -- 交割单口径用原始行号做同日先后排序；系统口径恒为 0
    seq                 INTEGER NOT NULL DEFAULT 0,
    -- 交割单原始摘要：红股/送股/转增等 0 成本入账靠它区分类型
    memo                TEXT    NOT NULL DEFAULT '',
    -- 交易复盘：全部可选，留空不影响入账；随成交一起重放
    trade_logic         TEXT    NOT NULL DEFAULT '',
    target_price_mills  INTEGER NOT NULL DEFAULT 0 CHECK (target_price_mills >= 0),
    plan_type           TEXT    NOT NULL DEFAULT '',
    hold_period         TEXT    NOT NULL DEFAULT '',
    sell_reason         TEXT    NOT NULL DEFAULT '',
    strategy_changed    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_date ON trades (account_id, symbol, trade_date);

-- 持仓批次：每笔买入一行，永不合并；open_date 是红利税时间戳的唯一来源
CREATE TABLE IF NOT EXISTS trade_lots (
    account_id                INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    symbol                    TEXT    NOT NULL,
    lot_id                    INTEGER NOT NULL,
    open_date                 TEXT    NOT NULL,
    open_trade_id             INTEGER NOT NULL,
    qty_opened                INTEGER NOT NULL CHECK (qty_opened >= 0),
    qty_remaining             INTEGER NOT NULL CHECK (qty_remaining >= 0),
    cost_cents                INTEGER NOT NULL CHECK (cost_cents >= 0),
    cost_remaining_cents      INTEGER NOT NULL CHECK (cost_remaining_cents >= 0),
    pending_div_cents         INTEGER NOT NULL DEFAULT 0 CHECK (pending_div_cents >= 0),
    pending_div_shares_basis  INTEGER NOT NULL DEFAULT 0 CHECK (pending_div_shares_basis >= 0),
    -- 剩余股数累计已收现金分红（净额）：双口径展示的摊薄成本基数
    received_div_cents        INTEGER NOT NULL DEFAULT 0 CHECK (received_div_cents >= 0),
    PRIMARY KEY (account_id, symbol, lot_id),
    FOREIGN KEY (account_id, open_trade_id) REFERENCES trades (account_id, trade_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lots_fifo ON trade_lots (account_id, symbol, open_date, lot_id);

-- 卖出扣减明细：FIFO 审计链，可逐笔复盘
CREATE TABLE IF NOT EXISTS lot_consumptions (
    consumption_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id           INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    sell_trade_id        INTEGER NOT NULL,
    symbol               TEXT    NOT NULL,
    lot_id               INTEGER NOT NULL,
    qty                  INTEGER NOT NULL CHECK (qty > 0),
    gross_cents          INTEGER NOT NULL CHECK (gross_cents >= 0),
    fee_cents            INTEGER NOT NULL CHECK (fee_cents >= 0),
    cost_released_cents  INTEGER NOT NULL CHECK (cost_released_cents >= 0),
    dividend_gross_cents INTEGER NOT NULL DEFAULT 0,
    dividend_tax_cents   INTEGER NOT NULL DEFAULT 0,
    tax_rate             TEXT    NOT NULL DEFAULT '0',
    tax_bracket          TEXT    NOT NULL,
    realized_pnl_cents   INTEGER NOT NULL,
    FOREIGN KEY (account_id, sell_trade_id) REFERENCES trades (account_id, trade_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_consumptions_trade ON lot_consumptions (account_id, sell_trade_id);

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    symbol                TEXT    NOT NULL,
    ex_date               TEXT    NOT NULL,
    cash_per_share_mills  INTEGER NOT NULL DEFAULT 0 CHECK (cash_per_share_mills >= 0),
    bonus_ratio           TEXT    NOT NULL DEFAULT '0',
    transfer_ratio        TEXT    NOT NULL DEFAULT '0',
    pay_date              TEXT,
    ex_price_mills        INTEGER,
    dividend_gross_cents  INTEGER NOT NULL DEFAULT 0,
    qty_added             INTEGER NOT NULL DEFAULT 0,
    drift_cents           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, symbol, ex_date, cash_per_share_mills, bonus_ratio, transfer_ratio)
);

-- 批次级红利记录：除息日 PENDING，卖出时按 FIFO 持股期限补扣
CREATE TABLE IF NOT EXISTS dividend_records (
    account_id            INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    record_id             INTEGER NOT NULL,
    symbol                TEXT    NOT NULL,
    lot_id                INTEGER NOT NULL,
    open_date             TEXT    NOT NULL,
    ex_date               TEXT    NOT NULL,
    qty                   INTEGER NOT NULL CHECK (qty >= 0),
    gross_cents           INTEGER NOT NULL CHECK (gross_cents >= 0),
    remaining_gross_cents INTEGER NOT NULL CHECK (remaining_gross_cents >= 0),
    tax_rate              TEXT    NOT NULL DEFAULT '0',
    tax_cents             INTEGER NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
    status                TEXT    NOT NULL CHECK (status IN ('PENDING', 'WITHHELD', 'EXEMPT')),
    PRIMARY KEY (account_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_dividends_pending ON dividend_records (account_id, symbol, status);

-- 用户自定义交易参数：全账户共享的单行配置，费率以字符串存储避免浮点
CREATE TABLE IF NOT EXISTS fee_config (
    config_id              INTEGER PRIMARY KEY CHECK (config_id = 1),
    commission_rate        TEXT    NOT NULL,
    min_commission_cents   INTEGER NOT NULL CHECK (min_commission_cents >= 0),
    stamp_tax_rate         TEXT    NOT NULL,
    transfer_fee_rate      TEXT    NOT NULL,
    etf_stamp_tax_exempt   INTEGER NOT NULL DEFAULT 1 CHECK (etf_stamp_tax_exempt IN (0, 1)),
    etf_transfer_fee_exempt INTEGER NOT NULL DEFAULT 1 CHECK (etf_transfer_fee_exempt IN (0, 1)),
    updated_at             TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    account_id   INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    entry_id     INTEGER NOT NULL,
    entry_date   TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    ref_id       INTEGER NOT NULL DEFAULT 0,
    memo         TEXT    NOT NULL DEFAULT '',
    -- 交割单口径用原始行号做同日先后排序；系统口径恒为 0
    seq          INTEGER NOT NULL DEFAULT 0,
    -- 红利入账的成交数量：当次实际在册股数，按批次拆分时作分母
    qty          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_date ON cash_ledger (account_id, entry_date);

-- 行情为全市场共享数据，不区分账户
CREATE TABLE IF NOT EXISTS quotes (
    symbol      TEXT    NOT NULL,
    price_date  TEXT    NOT NULL,
    close_mills INTEGER NOT NULL CHECK (close_mills > 0),
    PRIMARY KEY (symbol, price_date)
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    account_id         INTEGER NOT NULL REFERENCES account (account_id) ON DELETE CASCADE,
    snap_date          TEXT    NOT NULL,
    cash_cents         INTEGER NOT NULL,
    market_value_cents INTEGER NOT NULL,
    total_asset_cents  INTEGER NOT NULL,
    day_return_bp      INTEGER NOT NULL,
    nav_scaled         INTEGER NOT NULL,
    net_deposit_cents  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, snap_date)
);
