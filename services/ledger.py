"""交易与公司行为编排：引擎算完立即同事务落库，失败则重载回滚。

写操作全程持有 data.db.WRITE_LOCK，避免 Streamlit 并发重跑互相覆盖事务。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from config.settings import (
    DEFAULT_DIVIDEND_TAX_CONFIG,
    DEFAULT_FEE_CONFIG,
    LEDGER_MODE_STATEMENT,
    FeeConfig,
)
from core.avg_cost import AverageCostPool
from core.corporate_actions import CorporateAction, CorporateActionResult, apply_corporate_action
from core.fees import SIDE_SELL, FeeBreakdown
from core.money import MILLS_PER_CENT, prorate, qty_price_to_cents, round_half_up, yuan_to_mills
from core.tax import holding_period_bracket
from core.trading import ENTRY_DEPOSIT, Portfolio, TradeRecord
from data.repositories import ZERO_DIVIDEND_TAX, Repository

# 重放时由账务引擎自己生成的分录类型；库里的这些分录不能再当现金事件重放，
# 否则会与重算结果重复。其余类型（出入金、交割单补入的红利/利息）如实回放。
_GENERATED_CASH_KINDS = {
    "TRADE_BUY",
    "TRADE_SELL",
    "DIVIDEND",
    "DIVIDEND_TAX",
    "BONUS_RESIDUE",
}

# 股息红利差异（红利税）挂在卖出之后结算：税单只认同这段时间内卖出的批次，
# 避免用很早以前的卖出解释后来才出现的税单。
TAX_MATCH_WINDOW_DAYS = 30

# 交易复盘字段：可选填写，重放时随成交一起搬运，绝不参与任何金额计算
TRADE_NOTE_FIELDS = (
    "trade_logic",
    "target_price_mills",
    "plan_type",
    "hold_period",
    "sell_reason",
    "strategy_changed",
)


def _apply_trade_notes(trade: TradeRecord, notes: dict | None) -> None:
    """把（可能为空的）复盘字段挂到重放产出的成交上。"""
    if not notes:
        return
    for field in TRADE_NOTE_FIELDS:
        if field in notes and notes[field] is not None:
            setattr(trade, field, notes[field])


def _symbol_from_memo(memo: str) -> str:
    """交割单现金流的备注写成「类型 代码 名称」，从中取 6 位证券代码。"""
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", memo or "")
    return match.group(0) if match else ""


def _allocate_lot_dividend(portfolio, symbol: str, cents: int) -> None:
    """把交割单口径的现金红利 / 红利税按当前持股数摊到未平仓批次。

    系统口径由公司行为直接写批次；交割单口径没有批次级红利记录，这里在重放时
    按除息时点各批次剩余股数比例分配，供「原始成本 / 摊薄成本」双口径展示。
    净额（红利 − 红利税）永久累积，卖出时随股数比例释放。
    """
    book = portfolio.books.get(symbol)
    if book is None or cents == 0:
        return
    lots = [lot for lot in book.open_lots if lot.qty_remaining > 0]
    total_qty = sum(lot.qty_remaining for lot in lots)
    if total_qty <= 0:
        return
    if cents > 0:
        allocated = 0
        for lot in lots[:-1]:
            share = prorate(cents, lot.qty_remaining, total_qty)
            lot.received_div_cents += share
            allocated += share
        lots[-1].received_div_cents += cents - allocated
        return
    # 红利税是分红的追回：按批次现有分红依次冲减，不允许为负
    remaining = -cents
    for lot in lots:
        if remaining <= 0:
            break
        share = min(remaining, lot.received_div_cents)
        lot.received_div_cents -= share
        remaining -= share


class LedgerService:
    def __init__(self, conn, account_id: int = 1) -> None:
        self.account_id = int(account_id)
        self.repo = Repository(conn, self.account_id)
        self.portfolio: Portfolio = self.repo.load_portfolio()
        # 最近一次交割单导入跳过的重复条数，供界面提示
        self.last_import_duplicates = 0
        # 最近一次导入实际写入的现金流水条数（仅现金流水的新交割单也能增量更新）
        self.last_import_cash_written = 0
        if not self.repo.has_cash_ledger():
            with self.repo.lock():
                for entry in self.portfolio.ledger:
                    self.repo.save_cash_entry(entry)
                self.repo.commit()

    # ---------- 口径 ----------
    def is_statement_mode(self) -> bool:
        return self.repo.is_statement_mode()

    def _require_system_mode(self, action: str) -> None:
        if self.repo.is_statement_mode():
            raise ValueError(
                f"该账户是「交割单口径」，{action}已停用；"
                "请导入新的交割单来更新账面数据。"
            )

    # ---------- 交易参数 ----------
    def current_fee_config(self) -> FeeConfig:
        return self.portfolio.fee_config

    def update_fee_config(self, config: FeeConfig) -> None:
        """持久化新费率并立即生效（只影响后续成交，历史流水保持原费用）。"""
        self.repo.save_fee_config(config)
        self.portfolio.fee_config = config

    # ---------- 交易 ----------
    def buy(
        self,
        symbol: str,
        trade_date: date,
        qty: int,
        price_yuan,
        notes: dict | None = None,
    ) -> TradeRecord:
        """买入后全量重放：新批次才能补上其在册期间已落库的分红送转。

        只做增量落账会让「先同步公司行为、后写入新批次」的新批次永远错过
        历史分红，因此买入走与删改相同的重放路径，账面任何时刻都自洽。
        `notes` 为可选的交易复盘字段，留空不影响账务。
        """
        self._require_system_mode("手动买入")
        trade = self._replay(
            deleted_ids=set(),
            edited={},
            added=[
                {
                    "side": "BUY",
                    "symbol": str(symbol).strip(),
                    "trade_date": trade_date,
                    "qty": int(qty),
                    "price_mills": yuan_to_mills(price_yuan),
                    "notes": dict(notes or {}),
                }
            ],
        )
        assert trade is not None  # 重放必然产出这笔买入
        return trade

    def sell(
        self,
        symbol: str,
        trade_date: date,
        qty: int,
        price_yuan,
        notes: dict | None = None,
    ) -> TradeRecord:
        """卖出同样走全量重放：已实现盈亏要按加权摊薄成本池重算。

        增量落账只能拿到 FIFO 批次结转的结果，与券商 APP 的「加权平均 + 分红
        抵扣」口径不一致，因此卖出后重建整本账，盈亏取重放结果。
        """
        self._require_system_mode("手动卖出")
        trade = self._replay(
            deleted_ids=set(),
            edited={},
            added=[
                {
                    "side": "SELL",
                    "symbol": str(symbol).strip(),
                    "trade_date": trade_date,
                    "qty": int(qty),
                    "price_mills": yuan_to_mills(price_yuan),
                    "notes": dict(notes or {}),
                }
            ],
        )
        assert trade is not None  # 重放必然产出这笔卖出
        return trade

    # ---------- 公司行为 ----------
    def apply_action(
        self,
        action: CorporateAction,
        prev_close_yuan,
        eligible_on: date | None = None,
    ) -> CorporateActionResult:
        """执行公司行为；`eligible_on` 用于自动同步历史事件时限定在册批次。"""
        self._require_system_mode("公司行为（分红送转）")
        prev_close_mills = yuan_to_mills(prev_close_yuan)
        with self.repo.lock():
            ledger_mark = len(self.portfolio.ledger)
            dividend_mark = len(self.portfolio.dividends)
            try:
                result = apply_corporate_action(
                    self.portfolio, action, prev_close_mills, eligible_on
                )
                self.repo.save_corporate_action(
                    symbol=action.symbol,
                    ex_date=action.ex_date,
                    cash_per_share_mills=action.cash_per_share_mills,
                    bonus_ratio=Decimal(action.bonus_ratio),
                    transfer_ratio=Decimal(action.transfer_ratio),
                    pay_date=action.pay_date,
                    ex_price_mills=result.ex_price_mills,
                    dividend_gross_cents=result.dividend_gross_cents,
                    qty_added=result.qty_added,
                    drift_cents=result.drift_cents,
                )
                self.repo.save_lots(action.symbol, self.portfolio.book(action.symbol).lots)
                for entry in self.portfolio.ledger[ledger_mark:]:
                    self.repo.save_cash_entry(entry)
                for record in self.portfolio.dividends[dividend_mark:]:
                    self.repo.save_dividend_record(record)
                self.repo.commit()
                return result
            except Exception:
                self.repo.rollback()
                self.portfolio = self.repo.load_portfolio()
                raise

    # ---------- 成交删改（全量重放） ----------
    def delete_trades(self, trade_ids) -> None:
        """物理删除成交，并按剩余流水重算批次、现金、红利与公司行为。

        记账工具必须允许纠错：删除后从初始本金与出入金出发，把剩余成交和
        既有公司行为事件按日期重新执行一遍，派生状态（批次/红利税/净值基准）
        全部重建。重放在内存中先行验证，失败时数据库分毫不动。
        """
        self._require_system_mode("删除成交")
        self._replay(deleted_ids={int(t) for t in trade_ids}, edited={})

    def update_trade(
        self,
        trade_id: int,
        *,
        symbol: str,
        trade_date: date,
        qty: int,
        price_yuan,
        notes: dict | None = None,
    ) -> None:
        """修改一笔成交的代码/日期/数量/价格（方向不可改），随后全量重算。

        `notes` 可选：一并改掉该笔成交的决策记录，传空字符串即清除。
        """
        self._require_system_mode("修改成交")
        patch = {
            "symbol": str(symbol).strip(),
            "trade_date": trade_date,
            "qty": int(qty),
            "price_mills": yuan_to_mills(price_yuan),
        }
        if notes is not None:
            patch["notes"] = dict(notes)
        self._replay(deleted_ids=set(), edited={int(trade_id): patch})

    def rebuild_all(self) -> None:
        """按库内成交与公司行为全量重算（用于旧口径分红状态的一次性迁移）。"""
        self._replay(deleted_ids=set(), edited={})

    def import_trades(
        self,
        trades: list[dict],
        cash_entries: list[dict] | None = None,
    ) -> int:
        """批量导入交割单成交：一次全量重放，FIFO 批次/现金/红利自动重算。

        两种口径的差别：
        - 系统计算口径：`trades` 只需 `side`/`symbol`/`trade_date`/`qty`/`price_mills`，
          入账费用始终由 `core.fees.calculate_fees` 按当前交易参数重算（费用唯一出口）；
        - 交割单口径：额外必须提供 `amount_cents`（发生金额），佣金、印花税、过户费
          直接采用交割单上的数值，现金按发生金额入账，红利税不再重算。

        `cash_entries` 是交割单里的非交易现金流水（红利入账、股息红利差异、利息归本，
        交割单口径下还包括银行转账与逆回购），每条含 `entry_date`、`amount_cents`、
        `kind`、`memo`。只重放成交而不补这些流水，会让现金在中途见底而误报「现金不足」。

        交割单口径下按「日期+方向+代码+数量+价格」自动跳过已存在的成交，方便用
        新交割单增量更新。返回本次实际写入条数；任何一笔不合法都会在写库前抛错。

        去重按「已存在条数」逐笔核销，而不是简单集合判断：同一段交割单里可能存在
        同一天、同方向、同代码、同数量、同价格的多笔真实成交，它们必须全部入账。
        """
        statement_mode = self.repo.account_ledger_mode() == LEDGER_MODE_STATEMENT
        existing_keys: dict[tuple, int] = {}
        existing_cash_keys: dict[tuple, int] = {}
        existing_cash_rows: list[dict] = []
        qty_backfilled = False
        seq_base = 0
        if statement_mode:
            for row in self.repo.list_trades(limit=1_000_000):
                key = (
                    row["trade_date"],
                    row["side"],
                    row["symbol"],
                    int(row["qty"]),
                    int(row["price_mills"]),
                )
                existing_keys[key] = existing_keys.get(key, 0) + 1
                seq_base = max(seq_base, int(row["seq"]) if "seq" in row.keys() else 0)
            # 再次导入同一段交割单时，红利/银行流水也不能重复入账
            for row in self.repo.list_cash_entries():
                seq_base = max(seq_base, int(row["seq"]) if "seq" in row.keys() else 0)
                if row["kind"] in _GENERATED_CASH_KINDS:
                    continue
                key = (
                    row["entry_date"],
                    int(row["amount_cents"]),
                    row["kind"],
                    row["memo"] or "",
                )
                existing_cash_keys[key] = existing_cash_keys.get(key, 0) + 1
                existing_cash_rows.append(
                    {
                        "key": key,
                        "entry_id": int(row["entry_id"]),
                        "qty": int(row["qty"]) if "qty" in row.keys() else 0,
                    }
                )

        normalized: list[dict] = []
        duplicate_count = 0
        for index, item in enumerate(trades, start=1):
            side = str(item.get("side", "")).strip().upper()
            if side not in ("BUY", "SELL"):
                raise ValueError(f"第 {index} 笔成交方向无效：{item.get('side')}")
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                raise ValueError(f"第 {index} 笔成交缺少证券代码")
            qty = int(item.get("qty", 0))
            if qty <= 0:
                raise ValueError(f"{symbol} 第 {index} 笔成交数量必须为正")
            # 红股/送转/新股入账：只有股数、没有成交价与现金流，按 0 成本批次入账
            share_entry = bool(item.get("share_entry"))
            price_mills = 0 if share_entry else int(item.get("price_mills", 0))
            if price_mills <= 0 and not share_entry:
                raise ValueError(f"{symbol} 第 {index} 笔成交价格必须为正")
            trade_date = item.get("trade_date")
            if isinstance(trade_date, datetime):
                trade_date = trade_date.date()
            if not isinstance(trade_date, date):
                raise ValueError(f"{symbol} 第 {index} 笔成交日期无效")

            key = (trade_date.isoformat(), side, symbol, qty, price_mills)
            if statement_mode:
                pending = existing_keys.get(key, 0)
                if pending > 0:
                    existing_keys[key] = pending - 1
                    duplicate_count += 1
                    continue

            entry: dict = {
                "side": side,
                "symbol": symbol,
                "trade_date": trade_date,
                "qty": qty,
                "price_mills": price_mills,
                "memo": str(item.get("memo") or ""),
            }
            if statement_mode:
                raw_seq = item.get("seq")
                entry["seq"] = seq_base + (int(raw_seq) if raw_seq is not None else index)
                if share_entry:
                    entry.update(
                        {
                            "share_entry": True,
                            "amount_cents": 0,
                            "commission_cents": 0,
                            "stamp_tax_cents": 0,
                            "transfer_fee_cents": 0,
                        }
                    )
                    normalized.append(entry)
                    continue
                amount_cents = item.get("amount_cents")
                if amount_cents is None:
                    raise ValueError(
                        f"{symbol} 第 {index} 笔成交缺少「发生金额」，"
                        "交割单口径无法入账；请换用含发生金额列的交割单"
                    )
                amount_cents = int(amount_cents)
                if amount_cents == 0:
                    raise ValueError(f"{symbol} 第 {index} 笔成交的发生金额不能为零")
                gross = qty_price_to_cents(qty, price_mills)
                # 分项费用按交割单原值；若文件没有费用列，则用发生金额与成交额反推
                fees = FeeBreakdown(
                    commission_cents=abs(int(item.get("commission_cents", 0))),
                    stamp_tax_cents=abs(int(item.get("stamp_tax_cents", 0))),
                    transfer_fee_cents=abs(int(item.get("transfer_fee_cents", 0))),
                )
                other_fee = abs(int(item.get("other_fee_cents", 0)))
                if fees.total_cents + other_fee == 0:
                    fee_total = (
                        max(0, abs(amount_cents) - gross)
                        if side == "BUY"
                        else max(0, gross - amount_cents)
                    )
                    fees = FeeBreakdown(commission_cents=fee_total)
                elif other_fee:
                    # 其他杂费没有独立字段，并入佣金，保证展示的总费用与发生金额一致
                    fees = FeeBreakdown(
                        commission_cents=fees.commission_cents + other_fee,
                        stamp_tax_cents=fees.stamp_tax_cents,
                        transfer_fee_cents=fees.transfer_fee_cents,
                    )
                entry["amount_cents"] = amount_cents
                entry["commission_cents"] = fees.commission_cents
                entry["stamp_tax_cents"] = fees.stamp_tax_cents
                entry["transfer_fee_cents"] = fees.transfer_fee_cents
            normalized.append(entry)

        normalized_cash: list[dict] = []
        for index, item in enumerate(cash_entries or [], start=1):
            entry_date = item.get("entry_date")
            if isinstance(entry_date, datetime):
                entry_date = entry_date.date()
            if not isinstance(entry_date, date):
                raise ValueError(f"第 {index} 笔现金流水日期无效")
            amount_cents = int(item.get("amount_cents", 0))
            if amount_cents == 0:
                raise ValueError(f"第 {index} 笔现金流水金额不能为零")
            kind = str(item.get("kind") or "STATEMENT_CASH")
            if kind == "REPO":
                # 逆回购本金是资金停放（仍计入可用资金），不再作为现金收支入账
                continue
            memo = str(item.get("memo") or "")
            cash_key = (entry_date.isoformat(), amount_cents, kind, memo)
            if statement_mode:
                pending_cash = existing_cash_keys.get(cash_key, 0)
                if pending_cash > 0:
                    existing_cash_keys[cash_key] = pending_cash - 1
                    duplicate_count += 1
                    item_qty = int(item.get("qty") or 0)
                    if item_qty > 0:
                        stale = next(
                            (
                                row
                                for row in existing_cash_rows
                                if row["key"] == cash_key and row["qty"] == 0
                            ),
                            None,
                        )
                        if stale is not None:
                            # 旧库的红利流水没有股数：这次导入把成交数量补上
                            self.repo.update_cash_entry_qty(stale["entry_id"], item_qty)
                            stale["qty"] = item_qty
                            qty_backfilled = True
                    continue
            item_cash: dict = {
                "entry_date": entry_date,
                "amount_cents": amount_cents,
                "kind": kind,
                "memo": memo,
                "qty": int(item.get("qty") or 0),
            }
            if statement_mode:
                raw_seq = item.get("seq")
                item_cash["seq"] = seq_base + (int(raw_seq) if raw_seq is not None else index)
            normalized_cash.append(item_cash)

        if not statement_mode:
            # 系统口径下初始本金已代表累计入金，交割单里的银行转账与回购资金不入账
            normalized_cash = [
                item
                for item in normalized_cash
                if item["kind"] not in ("DEPOSIT", "WITHDRAW", "REPO")
            ]

        self.last_import_duplicates = duplicate_count
        self.last_import_cash_written = len(normalized_cash)
        if not normalized and not normalized_cash:
            if qty_backfilled:
                self.repo.commit()
            return 0
        self._replay(
            deleted_ids=set(),
            edited={},
            added=normalized,
            added_cash=normalized_cash,
        )
        return len(normalized)

    def _replay(
        self,
        deleted_ids: set[int],
        edited: dict[int, dict],
        added: list[dict] | None = None,
        added_cash: list[dict] | None = None,
    ) -> TradeRecord | None:
        initial = self.repo.initial_capital_cents()
        fee_config = self.repo.load_fee_config()
        statement_mode = self.repo.account_ledger_mode() == LEDGER_MODE_STATEMENT
        new_trades = list(added or [])
        new_cash = list(added_cash or [])

        # 1) 事件源：剩余成交 + 既有公司行为 + 用户出入金
        #    系统口径按「同日先资金、再除权、后成交」；交割单口径严格按原始行号，
        #    否则同日先卖出后逆回购这类真实顺序会被打乱，导致误报现金不足。
        events: list[tuple[date, int, int, dict]] = []
        for row in self.repo.list_trades(limit=1_000_000):
            trade_id = int(row["trade_id"])
            if trade_id in deleted_ids:
                continue
            patch = edited.get(trade_id)
            event = {
                "kind": "trade",
                "side": row["side"],
                "symbol": patch["symbol"] if patch else row["symbol"],
                "qty": patch["qty"] if patch else int(row["qty"]),
                "price_mills": patch["price_mills"] if patch else int(row["price_mills"]),
                "memo": row["memo"] if "memo" in row.keys() else "",
                # 复盘字段随事件搬运：即使重放后 trade_id 变化，内容也不丢
                # 修改成交时可整组覆盖（patch 里带 notes）
                "notes": (
                    dict(patch["notes"])
                    if patch and "notes" in patch
                    else {
                        field: row[field]
                        for field in TRADE_NOTE_FIELDS
                        if field in row.keys()
                    }
                ),
            }
            if statement_mode:
                # 交割单口径：费用与净现金流以库里的原值为准，绝不重算
                event["fees"] = FeeBreakdown(
                    commission_cents=int(row["commission_cents"]),
                    stamp_tax_cents=int(row["stamp_tax_cents"]),
                    transfer_fee_cents=int(row["transfer_fee_cents"]),
                )
                event["net_cash_cents"] = int(row["net_cash_cents"])
            events.append((
                patch["trade_date"] if patch else date.fromisoformat(row["trade_date"]),
                int(row["seq"]) if "seq" in row.keys() else 0,
                2,
                event,
            ))
        if not statement_mode:
            # 交割单口径不认公司行为：分红/送转/红利税全部来自交割单流水
            for row in self.repo.list_corporate_actions():
                events.append(
                    (
                        date.fromisoformat(row["ex_date"]),
                        0,
                        1,
                        {"kind": "action", "row": row},
                    )
                )
        for row in self.repo.list_cash_entries():
            if row["kind"] == "REPO":
                continue  # 逆回购本金不进入现金重放
            if row["kind"] in _GENERATED_CASH_KINDS or row["entry_date"] == "1900-01-01":
                continue
            events.append(
                (
                    date.fromisoformat(row["entry_date"]),
                    int(row["seq"]) if "seq" in row.keys() else 0,
                    0,
                    {"kind": "cash", "row": row},
                )
            )
        for item in new_cash:
            if item["kind"] == "REPO":
                continue
            events.append(
                (
                    item["entry_date"],
                    int(item.get("seq", 0)),
                    0,
                    {
                        "kind": "cash",
                        "row": {
                            "amount_cents": item["amount_cents"],
                            "kind": item["kind"],
                            "memo": item["memo"],
                            "seq": int(item.get("seq", 0)),
                            "qty": int(item.get("qty") or 0),
                        },
                    },
                )
            )
        for item in new_trades:
            event = {
                "kind": "trade",
                "side": item["side"],
                "symbol": item["symbol"],
                "qty": item["qty"],
                "price_mills": item["price_mills"],
                "memo": str(item.get("memo") or ""),
                "notes": dict(item.get("notes") or {}),
                "added": True,
            }
            if statement_mode:
                event["fees"] = FeeBreakdown(
                    commission_cents=int(item.get("commission_cents", 0)),
                    stamp_tax_cents=int(item.get("stamp_tax_cents", 0)),
                    transfer_fee_cents=int(item.get("transfer_fee_cents", 0)),
                )
                event["net_cash_cents"] = int(item["amount_cents"])
            events.append(
                (item["trade_date"], int(item.get("seq", 0)), 2, event)
            )
        if statement_mode:
            events.sort(key=lambda item: (item[0], item[1], item[2]))
        else:
            events.sort(key=lambda item: (item[0], item[2], item[1]))

        # 2) 纯内存重放：任何一步失败，数据库保持原样
        pf = Portfolio(
            initial_cash_cents=initial,
            fee_config=fee_config,
            tax_config=ZERO_DIVIDEND_TAX if statement_mode else DEFAULT_DIVIDEND_TAX_CONFIG,
        )
        replayed_actions: list[tuple[CorporateAction, CorporateActionResult]] = []
        added_trade: TradeRecord | None = None
        # 券商口径的已实现盈亏：移动加权平均成本池（分红抵扣成本）。
        # 批次账本仍按 FIFO 消耗，负责红利税档位与持仓明细，二者互不替代。
        avg_pool = AverageCostPool()
        for day, seq, _order, event in events:
            kind = event["kind"]
            if kind == "cash":
                row = event["row"]
                amount = int(row["amount_cents"])
                if row["kind"] == "WITHDRAW":
                    amount = -abs(amount)
                    if -amount > pf.cash_cents:
                        raise ValueError(
                            f"删改后 {day} 出金 {-amount / 100:,.2f} 元将超过当时可用现金"
                        )
                pf.post_cash(
                    day,
                    amount,
                    row["kind"],
                    0,
                    row["memo"] or "",
                    seq=seq,
                    qty=int(row["qty"]) if "qty" in row.keys() else 0,
                )
                if row["kind"] in ("STATEMENT_DIVIDEND", "STATEMENT_DIVIDEND_TAX"):
                    # 交割单红利抵扣成本；股息红利税为负数，等价于把红利吐回去
                    symbol = _symbol_from_memo(row["memo"])
                    avg_pool.reduce_cost(symbol, amount)
                    _allocate_lot_dividend(pf, symbol, amount)
            elif kind == "trade":
                overrides: dict = {}
                if statement_mode:
                    overrides["fees"] = event["fees"]
                    overrides["net_cash_cents"] = event["net_cash_cents"]
                    overrides["seq"] = seq
                if event["side"] == "BUY":
                    trade = pf.buy(
                        event["symbol"],
                        day,
                        event["qty"],
                        event["price_mills"],
                        **overrides,
                        memo=event.get("memo", ""),
                    )
                    avg_pool.buy(event["symbol"], trade.qty, abs(trade.net_cash_cents))
                else:
                    trade = pf.sell(
                        event["symbol"], day, event["qty"], event["price_mills"], **overrides
                    )
                    # 已实现盈亏 = 卖出净额（已扣费用）− 加权摊薄成本结转
                    released = avg_pool.release(event["symbol"], trade.qty)
                    trade.realized_pnl_cents = trade.net_cash_cents - released
                    if trade.dividend_tax_cents:
                        # 卖出时补扣的红利税是分红的追回，加回剩余持仓成本
                        avg_pool.reduce_cost(event["symbol"], -trade.dividend_tax_cents)
                _apply_trade_notes(trade, event.get("notes"))
                if event.get("added"):
                    added_trade = trade
            else:
                row = event["row"]
                action = CorporateAction(
                    symbol=row["symbol"],
                    ex_date=date.fromisoformat(row["ex_date"]),
                    cash_per_share_mills=int(row["cash_per_share_mills"]),
                    bonus_ratio=Decimal(row["bonus_ratio"]),
                    transfer_ratio=Decimal(row["transfer_ratio"]),
                    pay_date=date.fromisoformat(row["pay_date"]) if row["pay_date"] else None,
                )
                # 除权参考价反推前收盘价：prev = ex × (1 + 送转比例) + 每股派息
                prev_close_mills = round_half_up(
                    Decimal(int(row["ex_price_mills"])) * (1 + action.share_ratio)
                    + action.cash_per_share_mills
                )
                result = apply_corporate_action(pf, action, prev_close_mills, action.ex_date)
                replayed_actions.append((action, result))
                # 送转只加股数；现金分红抵扣成本（与债券派息、红利税分别处理）
                avg_pool.add_shares(action.symbol, result.qty_added)
                if result.dividend_gross_cents:
                    avg_pool.reduce_cost(action.symbol, result.dividend_gross_cents)

        # 3) 单事务清空重写，保证「要么全部重算成功，要么维持原账」
        with self.repo.lock():
            try:
                self.repo.wipe_ledger_data()
                for trade in pf.trades:
                    self.repo.save_trade(trade)
                for symbol, book in pf.books.items():
                    self.repo.save_lots(symbol, book.lots)
                for entry in pf.ledger:
                    self.repo.save_cash_entry(entry)
                symbols_by_trade = {trade.trade_id: trade.symbol for trade in pf.trades}
                for sell_id, item in pf.consumptions:
                    self.repo.save_consumption(
                        sell_id, symbols_by_trade[sell_id], item.lot_id, item
                    )
                for record in pf.dividends:
                    self.repo.save_dividend_record(record)
                for action, result in replayed_actions:
                    self.repo.save_corporate_action(
                        symbol=action.symbol,
                        ex_date=action.ex_date,
                        cash_per_share_mills=action.cash_per_share_mills,
                        bonus_ratio=Decimal(action.bonus_ratio),
                        transfer_ratio=Decimal(action.transfer_ratio),
                        pay_date=action.pay_date,
                        ex_price_mills=result.ex_price_mills,
                        dividend_gross_cents=result.dividend_gross_cents,
                        qty_added=result.qty_added,
                        drift_cents=result.drift_cents,
                    )
                self.repo.commit()
                self.portfolio = self.repo.load_portfolio()
            except Exception:
                self.repo.rollback()
                self.portfolio = self.repo.load_portfolio()
                raise
        return added_trade

    # ---------- 行情 ----------
    def set_close(self, symbol: str, price_date: date, close_yuan) -> None:
        with self.repo.lock():
            self.repo.upsert_quote(symbol, price_date, yuan_to_mills(close_yuan))
            self.repo.commit()

    # ---------- 账户资金 ----------
    def initial_capital_cents(self) -> int:
        return self.repo.initial_capital_cents()

    def capital_base_cents(self) -> int:
        """净值基准：已填初始本金优先；未填时按现金流水里的入金 / 出金累计。

        交割单口径的期初现金一般填 0，真实本金来自交割单里的「银行转证券 /
        证券转银行」流水（导入时记为 DEPOSIT / WITHDRAW），累计后即为投入本金，
        用于总资产卡片换算净值。
        """
        initial = self.repo.initial_capital_cents()
        if initial > 0:
            return initial
        return sum(
            entry.amount_cents
            for entry in self.portfolio.ledger
            if entry.kind in ("DEPOSIT", "WITHDRAW")
        )

    def set_initial_capital(self, amount_cents: int) -> None:
        """修改初始本金。

        - 交割单口径：初始本金就是期初现金，修改后必须同步期初流水，否则现金对不上；
        - 系统口径且无交易流水：同步改写期初入金流水，现金随之变化；
        - 系统口径已有交易流水：只调整净值/收益率基准，绝不动现有现金与历史流水。
        所有情况下旧净值快照都会失效并被清空。
        """
        if amount_cents < 0:
            raise ValueError("初始本金不能为负")
        update_cash = self.repo.is_statement_mode() or not self.repo.has_trades()

        with self.repo.lock():
            try:
                self.repo.update_initial_capital(amount_cents)
                if update_cash:
                    entry = self.repo.synthetic_initial_entry()
                    if entry is not None:
                        self.repo.update_cash_entry_amount(int(entry["entry_id"]), amount_cents)
                    elif amount_cents > 0:
                        new_entry = self.portfolio.post_cash(
                            date(1900, 1, 1), amount_cents, ENTRY_DEPOSIT, 0, "初始入金"
                        )
                        self.repo.save_cash_entry(new_entry)
                self.repo.clear_snapshots()
                self.repo.commit()
                self.portfolio = self.repo.load_portfolio()
            except Exception:
                self.repo.rollback()
                self.portfolio = self.repo.load_portfolio()
                raise

    def deposit(self, amount_cents: int, entry_date: date | None = None):
        """入金：追加一笔资金流水，不改变初始本金与历史收益率基准。"""
        self._require_system_mode("手动入金")
        if amount_cents <= 0:
            raise ValueError("入金金额必须为正")
        return self._cash_entry(amount_cents, "DEPOSIT", "入金", entry_date)

    def withdraw(self, amount_cents: int, entry_date: date | None = None):
        """出金：从可用现金中划出，金额上限为当前现金。"""
        self._require_system_mode("手动出金")
        if amount_cents <= 0:
            raise ValueError("出金金额必须为正")
        if amount_cents > self.portfolio.cash_cents:
            raise ValueError(
                f"现金不足：可用 {self.portfolio.cash_cents} 分，试图出金 {amount_cents} 分"
            )
        return self._cash_entry(-amount_cents, "WITHDRAW", "出金", entry_date)

    def _cash_entry(self, amount_cents: int, kind: str, label: str, entry_date=None):
        if amount_cents == 0:
            raise ValueError("出入金金额必须非零")
        when = entry_date or date.today()
        with self.repo.lock():
            try:
                entry = self.portfolio.post_cash(when, amount_cents, kind, 0, label)
                self.repo.save_cash_entry(entry)
                self.repo.commit()
                return entry
            except Exception:
                self.repo.rollback()
                self.portfolio = self.repo.load_portfolio()
                raise

    # ---------- 只读视图 ----------
    def _symbol_dividend_gross_cents(
        self, symbol: str, since: date | None = None
    ) -> int:
        """该标的累计已收税前现金分红（券商成本价口径，不扣红利税）。

        `since` 用于整仓清空后的重新开仓：只统计本轮建仓之后的红利。
        """
        if self.is_statement_mode():
            total = 0
            for entry in self.portfolio.ledger:
                if entry.kind != "STATEMENT_DIVIDEND":
                    continue
                if _symbol_from_memo(entry.memo) != symbol:
                    continue
                if since is not None and entry.entry_date <= since:
                    continue
                total += entry.amount_cents
            return total
        return sum(
            record.gross_cents
            for record in self.portfolio.dividends
            if record.symbol == symbol
            and (since is None or record.ex_date > since)
        )

    def _broker_cost_cents(self, symbol: str) -> int:
        """券商 App 的「成本价」基数：累计买入含费 − 累计卖出净额 − 累计税前分红。

        与同花顺一致：把历史卖出盈亏与历史分红都摊到当前剩余股数上，
        得到的是一股「保本价」，而不是剩余批次的买入成本。
        但整仓清空后重新开仓时成本池归零：券商把上一轮的已实现盈亏直接结算掉，
        不会让它继续摊薄新一轮持仓成本（否则清仓再买会让成本价失真）。
        """
        cost_cents = 0
        qty = 0
        since: date | None = None
        for trade in sorted(
            self.portfolio.trades, key=lambda t: (t.trade_date, t.seq, t.trade_id)
        ):
            if trade.symbol != symbol:
                continue
            if trade.side == "BUY":
                cost_cents += -trade.net_cash_cents
                qty += trade.qty
            elif trade.side == "SELL":
                cost_cents -= trade.net_cash_cents
                qty -= trade.qty
            if qty <= 0:
                # 整仓清空：上一轮的成本与盈亏到此结算
                cost_cents = 0
                qty = 0
                since = trade.trade_date
        return cost_cents - self._symbol_dividend_gross_cents(symbol, since)

    def holdings_summary(self, as_of: date | None = None) -> list[dict]:
        """按代码聚合未平仓批次：原始成本、券商摊薄成本、累计分红与持仓盈亏。

        双口径：
        - 原始成本：剩余批次的买入真实总成本（含费用），只在送转时摊薄；
        - 摊薄成本：券商口径保本价 =（累计买入含费 − 累计卖出净额 − 累计税前分红）
          ÷ 当前股数，把已实现盈亏也摊进成本；
        - 持仓盈亏 = 市值 − 摊薄成本 × 股数，含已实现盈亏，与券商 App 一致。
        """
        today = as_of or date.today()
        rows = []
        for symbol in sorted(self.portfolio.books):
            book = self.portfolio.books[symbol]
            qty = book.total_qty
            if qty <= 0:
                continue
            cost_cents = book.cost_remaining_cents
            dividend_cents = self._symbol_dividend_gross_cents(symbol)
            broker_cost_cents = self._broker_cost_cents(symbol)
            avg_mills = round_half_up(
                Decimal(cost_cents) * MILLS_PER_CENT / Decimal(qty)
            )
            diluted_mills = round_half_up(
                Decimal(broker_cost_cents) * MILLS_PER_CENT / Decimal(qty)
            )
            price = self.repo.latest_price(symbol, today)
            market_cents = qty_price_to_cents(qty, price) if price is not None else None
            pnl_cents = (
                market_cents - broker_cost_cents if market_cents is not None else None
            )
            ratio = None
            if pnl_cents is not None and broker_cost_cents > 0:
                ratio = round(pnl_cents * 100 / broker_cost_cents, 2)
            rows.append(
                {
                    "代码": symbol,
                    "持仓股数": qty,
                    "原始成本(元/股)": round(avg_mills / 1000, 3),
                    "摊薄成本(元/股)": round(diluted_mills / 1000, 3),
                    "原始成本(元)": round(cost_cents / 100, 2),
                    "累计现金分红(元)": round(dividend_cents / 100, 2),
                    "最新价(元)": round(price / 1000, 3) if price is not None else None,
                    "市值(元)": round(market_cents / 100, 2) if market_cents is not None else None,
                    "持仓盈亏(元)": round(pnl_cents / 100, 2) if pnl_cents is not None else None,
                    "盈亏比例(%)": ratio,
                }
            )
        return rows

    def _today_rate(self, book, lot) -> Decimal:
        """该批次以「今日」为判定日的红利税率：开仓已满 1 年（或 ETF）为 0。

        持股期限只增不减，一旦今日已满 1 年，未来任何时点卖出都免税；
        因此对待缴红利的展示与待缴基数都用今日口径动态计算，而非冻结在除权日。
        """
        return holding_period_bracket(
            lot.open_date, date.today(), book.asset_type, self.portfolio.tax_config
        )[0]

    def positions(self) -> list[dict]:
        """持仓批次明细：同时给出「原始成本」（锁定）与「批次摊薄成本」（扣本批分红/送转）。

        批次视角只反映单批剩余成本，不摊入其他批次或历史卖出盈亏；账户级券商口径
        请用 `holdings_summary()`。
        待缴红利按「今日」持股期限动态判定：开仓满 1 年（或 ETF）即不再计入。
        """
        rows = []
        for symbol, book in self.portfolio.books.items():
            for lot in book.lots:
                if lot.qty_remaining <= 0:
                    continue
                pending = lot.pending_div_cents if self._today_rate(book, lot) > 0 else 0
                rows.append(
                    {
                        "代码": symbol,
                        "批次": lot.lot_id,
                        "买入日": lot.open_date.isoformat(),
                        "持股天数": (date.today() - lot.open_date).days,
                        "剩余股数": lot.qty_remaining,
                        "买入原价(元/股)": round(lot.buy_price_mills / 1000, 3),
                        "原始成本(元/股)": round(lot.original_cost_mills / 1000, 3),
                        "批次摊薄成本(元/股)": round(lot.diluted_cost_mills / 1000, 3),
                        "剩余成本(元)": round(lot.cost_remaining_cents / 100, 2),
                        "累计分红(元)": round(lot.received_div_cents / 100, 2),
                        "待缴红利(元)": round(pending / 100, 2),
                    }
                )
        return rows

    def pending_dividend_total(self) -> int:
        """当前待缴红利基数（分）：仅计入按今日口径仍可能被扣税的批次。

        开仓满 1 年（或 ETF）的批次，未来卖出必为免税，不再计入待缴。
        """
        total = 0
        for book in self.portfolio.books.values():
            for lot in book.open_lots:
                if self._today_rate(book, lot) > 0:
                    total += lot.pending_div_cents
        return total

    def lot_corporate_actions(self, symbol: str | None = None, lot_id: int | None = None) -> list[dict]:
        """查询批次关联的公司行为明细：每股派息、送转比例、本批次红利与状态。

        只展示在册批次；红利状态按「今日」持股期限动态判定——开仓满 1 年（或
        ETF）的 PENDING 记录即时翻成免税，不再永久停在待缴（除非已在卖出时扣缴）。
        """
        rows = []
        for sym, book in sorted(self.portfolio.books.items()):
            if symbol is not None and sym != symbol:
                continue
            actions = self.repo.list_corporate_actions(sym)
            for lot in book.open_lots:
                if lot_id is not None and lot.lot_id != lot_id:
                    continue
                dividends = {
                    row["ex_date"]: row
                    for row in self.repo.dividend_records_for_lot(sym, lot.lot_id)
                }
                rate = self._today_rate(book, lot)
                for action in actions:
                    if action["ex_date"] < lot.open_date.isoformat():
                        continue
                    cash = int(action["cash_per_share_mills"])
                    bonus = Decimal(action["bonus_ratio"])
                    transfer = Decimal(action["transfer_ratio"])
                    if cash > 0 and (bonus > 0 or transfer > 0):
                        kind = "分红+送转"
                    elif cash > 0:
                        kind = "现金分红"
                    else:
                        kind = "送转股"
                    record = dividends.get(action["ex_date"])
                    status = "未参与"
                    if record is not None:
                        if record["status"] == "PENDING":
                            status = "EXEMPT" if rate == 0 else "PENDING"
                        else:
                            status = record["status"]
                    rows.append(
                        {
                            "代码": sym,
                            "批次": lot.lot_id,
                            "买入日": lot.open_date.isoformat(),
                            "除权日": action["ex_date"],
                            "类型": kind,
                            "股数": int(record["qty"]) if record else 0,
                            "每股派息(元)": round(cash / 1000, 3),
                            "每股送股": float(bonus),
                            "每股转增": float(transfer),
                            "本批次红利(元)": round(record["gross_cents"] / 100, 2) if record else 0.0,
                            "红利状态": status,
                        }
                    )
        # 按除权日倒序、批次升序，让最新的派息排在前，阅读更直观
        rows.sort(key=lambda r: (r["除权日"], -int(r["批次"])), reverse=True)
        return rows

    def statement_corporate_actions(
        self, symbol: str | None = None, *, batch_level: bool = False
    ) -> list[dict]:
        """交割单口径的公司行为：从导入的红利流水与 0 成本红股批次派生。

        交割单口径不写 `corporate_actions` 表（否则重放会重复计账），
        因此这里在读取时派生，仅供展示与统计：
        - 红利 / 股息红利差异 → 现金流水（STATEMENT_DIVIDEND / _TAX）
        - 红股 / 送转 / 新股入账 → `price_mills = 0` 的买入成交
        代码从流水备注里取（导入时写成「类型 代码 名称」）。

        `batch_level=True` 时按批次拆分，列项与系统口径完全一致：
        红利按事件发生日各批次在册股数分摊并反推每股派息；送转按除权前在册
        股数分摊，比例 = 新增股数 ÷ 在册股数；红利税不单独成行，只把对应批次的
        状态标记为「已扣缴」。
        """
        if not self.is_statement_mode():
            return []
        if not batch_level:
            return self._statement_actions_summary(symbol)
        return self._statement_actions_by_lot(symbol)

    def _statement_actions_summary(self, symbol: str | None) -> list[dict]:
        """逐笔汇总行：供「公司行为」页展示，保留原有列结构。"""
        rows: list[dict] = []
        for row in self.repo.list_cash_entries():
            kind = row["kind"]
            if kind not in ("STATEMENT_DIVIDEND", "STATEMENT_DIVIDEND_TAX"):
                continue
            memo = row["memo"] or ""
            code = _symbol_from_memo(memo)
            if symbol is not None and code != symbol:
                continue
            rows.append(
                {
                    "代码": code,
                    "除权日": row["entry_date"],
                    "类型": "股息红利税" if kind == "STATEMENT_DIVIDEND_TAX" else "现金分红",
                    "派现(元)": int(row["amount_cents"]) / 100,
                    "新增股数": 0,
                    "说明": memo,
                }
            )
        for trade in self.portfolio.trades:
            if trade.side != "BUY" or trade.price_mills != 0:
                continue
            if symbol is not None and trade.symbol != symbol:
                continue
            rows.append(
                {
                    "代码": trade.symbol,
                    "除权日": trade.trade_date.isoformat(),
                    "类型": "红股/送转入账",
                    "派现(元)": 0.0,
                    "新增股数": trade.qty,
                    "说明": "0 成本入账，不产生现金流",
                }
            )
        rows.sort(key=lambda r: (r["除权日"], r["类型"]), reverse=True)
        return rows

    def _statement_actions_by_lot(self, symbol: str | None) -> list[dict]:
        """按批次拆分的交割单公司行为（列项与系统口径一致）。"""
        rows, _facts = self._statement_action_detail(symbol)
        return rows

    def dividend_facts(self, symbol: str) -> dict[int, dict]:
        """按批次汇总历史红利事实：税前累计、已扣缴红利税、红利状态。

        供「标的明细 → 买卖明细」在买入行展示。交割单口径同样从红利流水与
        税单派生，两种口径的列项一致。`symbol` 必须指定，返回值按批次编号索引。
        """
        if self.is_statement_mode():
            _rows, facts = self._statement_action_detail(symbol)
            return facts
        facts: dict[int, dict] = {}
        book = self.portfolio.books.get(symbol)
        if book is None:
            return facts
        for lot in book.lots:
            records = self.repo.dividend_records_for_lot(symbol, lot.lot_id)
            statuses: list[str] = []
            for record in records:
                status = str(record["status"])
                # 待缴按「今日」持股期限动态翻转：开仓满一年即为免税
                if status == "PENDING" and self._today_rate(book, lot) == 0:
                    status = "EXEMPT"
                if status not in statuses:
                    statuses.append(status)
            facts[lot.lot_id] = {
                "gross_cents": sum(int(record["gross_cents"]) for record in records),
                "tax_cents": sum(int(record["tax_cents"]) for record in records),
                "statuses": statuses,
            }
        return facts

    def _statement_action_detail(
        self, symbol: str | None
    ) -> tuple[list[dict], dict[int, dict]]:
        """按批次拆分的交割单公司行为，同时给出每批次红利汇总。"""
        rows: list[dict] = []
        facts: dict[int, dict] = {}
        symbols = [symbol] if symbol is not None else sorted(self.portfolio.books)
        for sym in symbols:
            book = self.portfolio.books.get(sym)
            if book is None:
                continue
            consumed: dict[int, list[tuple[date, int]]] = {}
            sold_lots_by_sell: dict[int, set[int]] = {}
            closed_on: dict[int, date] = {}
            qty_used: dict[int, int] = {}
            lots_by_id = {lot.lot_id: lot for lot in book.lots}
            for item in self.repo.list_timed_consumptions(sym):
                lot_id = int(item["lot_id"])
                sell_day = date.fromisoformat(item["sell_date"])
                qty = int(item["qty"])
                consumed.setdefault(lot_id, []).append((sell_day, qty))
                sold_lots_by_sell.setdefault(int(item["sell_trade_id"]), set()).add(lot_id)
                qty_used[lot_id] = qty_used.get(lot_id, 0) + qty
                lot = lots_by_id.get(lot_id)
                if (
                    lot is not None
                    and lot_id not in closed_on
                    and qty_used[lot_id] >= lot.qty_opened
                ):
                    closed_on[lot_id] = sell_day

            def qty_on(lot, day: date) -> int:
                """除权日 / 派息日的在册股数：当日卖出仍算在册（先除权后成交）。"""
                used = sum(q for sell_day, q in consumed.get(lot.lot_id, []) if sell_day < day)
                return max(0, lot.qty_opened - used)

            def register(day: date, inclusive: bool = False) -> dict[int, tuple[object, int]]:
                result: dict[int, tuple[object, int]] = {}
                for lot in book.lots:
                    if lot.open_date > day or (lot.open_date == day and not inclusive):
                        continue
                    qty = qty_on(lot, day)
                    if qty > 0:
                        result[lot.lot_id] = (lot, qty)
                return result

            # 1) 现金红利：按在册股数分到批次，反推每股派息。
            # 先算出每批次在各派息日的税前份额，红利税再按份额分摊回去。
            dividend_items: list[dict] = []
            for entry in self.portfolio.ledger:
                if entry.kind != "STATEMENT_DIVIDEND":
                    continue
                if _symbol_from_memo(entry.memo) != sym:
                    continue
                holding = register(entry.entry_date)
                # 交割单「成交数量」就是当次实际在册股数，优先用它做分母；
                # 同日买入在红利到账日通常已完成登记，按需补进同日批次对齐股数。
                declared_qty = int(getattr(entry, "qty", 0) or 0)
                if declared_qty > sum(qty for _lot, qty in holding.values()):
                    for lot_id, (lot, qty) in register(
                        entry.entry_date, inclusive=True
                    ).items():
                        if lot_id in holding:
                            continue
                        holding[lot_id] = (lot, qty)
                        if declared_qty <= sum(q for _l, q in holding.values()):
                            break
                total_qty = sum(qty for _lot, qty in holding.values())
                amount = int(entry.amount_cents)
                if total_qty <= 0 or amount <= 0:
                    continue
                share_qty = declared_qty if declared_qty > 0 else total_qty
                per_share_mills = round_half_up(
                    Decimal(amount * MILLS_PER_CENT) / Decimal(share_qty)
                )
                allocated = 0
                items = list(holding.items())
                for index, (lot_id, (lot, qty)) in enumerate(items):
                    share = (
                        amount - allocated
                        if index == len(items) - 1
                        else prorate(amount, qty, total_qty)
                    )
                    allocated += share
                    dividend_items.append(
                        {
                            "lot_id": lot_id,
                            "lot": lot,
                            "day": entry.entry_date,
                            "qty": qty,
                            "share_cents": share,
                            "per_share_mills": per_share_mills,
                            "memo": entry.memo,
                        }
                    )

            # 2) 红利税是卖出后由中登补扣的：税单挂到它之前 30 天内卖出的批次，
            # 只有这些实际被卖出的批次才算「已扣缴」；没卖出的批次继续按持股期限
            # 判断（满一年免税），不会因为同标的别的批次卖出就误标成已扣缴。
            tax_entries = sorted(
                (
                    entry
                    for entry in self.portfolio.ledger
                    if entry.kind == "STATEMENT_DIVIDEND_TAX"
                    and _symbol_from_memo(entry.memo) == sym
                ),
                key=lambda entry: (entry.entry_date, entry.seq),
            )
            sells = sorted(
                (
                    trade
                    for trade in self.portfolio.trades
                    if trade.symbol == sym and trade.side == SIDE_SELL
                ),
                key=lambda trade: (trade.trade_date, trade.seq, trade.trade_id),
            )
            tax_by_lot: dict[int, int] = {}
            withheld_after: dict[int, date] = {}
            unmatched_taxes: list[tuple[date, int]] = []
            gross_before: dict[int, list[tuple[date, int]]] = {}
            for item in dividend_items:
                gross_before.setdefault(item["lot_id"], []).append(
                    (item["day"], item["share_cents"])
                )

            def add_tax(amount_cents: int, weights: dict[int, int]) -> None:
                """把一张税单按权重分摊到批次，尾差由最后一个批次吸收。"""
                if amount_cents <= 0 or not weights:
                    return
                pairs = list(weights.items())
                total = sum(max(0, weight) for _lot_id, weight in pairs)
                allocated = 0
                for index, (lot_id, weight) in enumerate(pairs):
                    if index == len(pairs) - 1:
                        share = amount_cents - allocated
                    elif total > 0:
                        share = prorate(amount_cents, max(0, weight), total)
                    else:
                        share = amount_cents // len(pairs)
                    allocated += share
                    tax_by_lot[lot_id] = tax_by_lot.get(lot_id, 0) + share

            for entry in tax_entries:
                # 股息红利差异是卖出后由中登补扣的，通常紧跟着卖出结算；
                # 同一笔卖出可能按批次拆成多张税单，因此允许同一笔卖出被重复挂靠。
                recent = [
                    trade
                    for trade in sells
                    if trade.trade_date <= entry.entry_date
                    and (entry.entry_date - trade.trade_date).days <= TAX_MATCH_WINDOW_DAYS
                ]
                sold_lots: set[int] = set()
                for trade in recent:
                    sold_lots |= sold_lots_by_sell.get(trade.trade_id, set())
                if not sold_lots:
                    unmatched_taxes.append(
                        (entry.entry_date, abs(int(entry.amount_cents)))
                    )
                    continue
                for lot_id in sold_lots:
                    if entry.entry_date > withheld_after.get(lot_id, date.min):
                        withheld_after[lot_id] = entry.entry_date
                # 权重取该批次在这张税单之前已收到的红利；都还没收过红利时均摊
                add_tax(
                    abs(int(entry.amount_cents)),
                    {
                        lot_id: sum(
                            share
                            for day, share in gross_before.get(lot_id, [])
                            if day <= entry.entry_date
                        )
                        for lot_id in sold_lots
                    },
                )

            # 有的券商在派息当日就扣税（而不是卖出时补扣）：这类税单离卖出很远，
            # 但紧挨着红利入账日，按「税单 ±7 天内最近的一次派息」归到当次在册批次。
            for tax_day, tax_cents in unmatched_taxes:
                near = [
                    item["day"]
                    for item in dividend_items
                    if abs((tax_day - item["day"]).days) <= 7
                ]
                if not near:
                    continue
                target = min(near)
                weights: dict[int, int] = {}
                for item in dividend_items:
                    if item["day"] != target:
                        continue
                    weights[item["lot_id"]] = (
                        weights.get(item["lot_id"], 0) + item["share_cents"]
                    )
                for lot_id in weights:
                    if tax_day > withheld_after.get(lot_id, date.min):
                        withheld_after[lot_id] = tax_day
                add_tax(tax_cents, weights)

            def dividend_status(lot, day: date) -> str:
                # 除权日已满一年（或 ETF）的红利本身免税，不会因为后来卖出而补扣
                ex_rate, _bracket = holding_period_bracket(
                    lot.open_date, day, book.asset_type, DEFAULT_DIVIDEND_TAX_CONFIG
                )
                if ex_rate > 0 and withheld_after.get(lot.lot_id, date.min) >= day:
                    return "WITHHELD"
                # 未实际扣税的批次看「今天」的持股期限：持有满一年就免税，
                # 与系统口径的 _today_rate 一致，不让去年红利永远停在待缴。
                reference = closed_on.get(lot.lot_id) or date.today()
                rate, _bracket = holding_period_bracket(
                    lot.open_date, reference, book.asset_type, DEFAULT_DIVIDEND_TAX_CONFIG
                )
                return "EXEMPT" if rate == 0 else "PENDING"

            # 3) 输出现金红利行，并累计每批次的税前红利与状态
            for item in dividend_items:
                lot = item["lot"]
                status = dividend_status(lot, item["day"])
                rows.append(
                    {
                        "代码": sym,
                        "批次": item["lot_id"],
                        "买入日": lot.open_date.isoformat(),
                        "除权日": item["day"].isoformat(),
                        "类型": "现金分红",
                        "股数": item["qty"],
                        "每股派息(元)": round(item["per_share_mills"] / 1000, 3),
                        "每股送股": 0.0,
                        "每股转增": 0.0,
                        "本批次红利(元)": round(item["share_cents"] / 100, 2),
                        "红利状态": status,
                        "派现(元)": item["share_cents"] / 100,
                        "新增股数": 0,
                        "说明": item["memo"],
                    }
                )
                fact = facts.setdefault(
                    item["lot_id"],
                    {"gross_cents": 0, "tax_cents": 0, "statuses": []},
                )
                fact["gross_cents"] += item["share_cents"]
                if status not in fact["statuses"]:
                    fact["statuses"].append(status)

            for lot_id, amount_cents in tax_by_lot.items():
                fact = facts.setdefault(
                    lot_id, {"gross_cents": 0, "tax_cents": 0, "statuses": []}
                )
                fact["tax_cents"] += amount_cents

            # 4) 送股 / 转增 / 新股入账：0 成本买入按除权前在册股数分摊
            for trade in self.portfolio.trades:
                if trade.symbol != sym or trade.side != "BUY" or trade.price_mills != 0:
                    continue
                holding = register(trade.trade_date)
                total_qty = sum(qty for _lot, qty in holding.values())
                memo = trade.memo or ""
                if "转增" in memo:
                    kind, bonus_side = "转增", "transfer"
                elif "送" in memo or "红股" in memo:
                    kind, bonus_side = "送股", "bonus"
                elif "新股" in memo or "配股" in memo:
                    kind, bonus_side = "新股入账", ""
                else:
                    kind, bonus_side = "送转股", "bonus"
                ratio = (
                    round_half_up(Decimal(trade.qty) * 1000 / Decimal(total_qty)) / 1000
                    if total_qty > 0
                    else 0.0
                )
                items = list(holding.items())
                if not items:
                    continue
                added_total = 0
                for index, (lot_id, (lot, qty)) in enumerate(items):
                    added = (
                        trade.qty - added_total
                        if index == len(items) - 1
                        else prorate(trade.qty, qty, total_qty)
                    )
                    added_total += added
                    rows.append(
                        {
                            "代码": sym,
                            "批次": lot_id,
                            "买入日": lot.open_date.isoformat(),
                            "除权日": trade.trade_date.isoformat(),
                            "类型": kind,
                            "股数": qty,
                            "每股派息(元)": 0.0,
                            "每股送股": ratio if bonus_side == "bonus" else 0.0,
                            "每股转增": ratio if bonus_side == "transfer" else 0.0,
                            "本批次红利(元)": 0.0,
                            "红利状态": "未参与",
                            "派现(元)": 0.0,
                            "新增股数": added,
                            "说明": memo or "0 成本入账，不产生现金流",
                        }
                    )
                    facts.setdefault(
                        lot_id, {"gross_cents": 0, "tax_cents": 0, "statuses": []}
                    )

            # 兜底闭合：税单金额必须逐分落到批次上，不能因为匹配不到卖出而消失。
            # 优先按批次已收红利占比分摊，完全没有红利时均摊。
            expected_tax = sum(abs(int(entry.amount_cents)) for entry in tax_entries)
            leftover = expected_tax - sum(tax_by_lot.values())
            if leftover > 0 and facts:
                weights = {
                    lot_id: fact["gross_cents"]
                    for lot_id, fact in facts.items()
                    if fact["gross_cents"] > 0
                }
                add_tax(leftover, weights or {lot_id: 1 for lot_id in facts})

            # 只参与过送转、没拿过现金红利的批次记「未参与」，买卖明细据此显示
            for fact in facts.values():
                if not fact["statuses"]:
                    fact["statuses"].append("未参与")

        rows.sort(key=lambda r: (r["除权日"], -int(r["批次"])), reverse=True)
        return rows, facts


MIGRATION_DIVIDEND_EXEMPT = "migration_dividend_exempt_v2"


MIGRATION_REALIZED_AVG_COST = "migration_realized_avg_cost_v1"

MIGRATION_LOT_DIVIDEND = "migration_lot_dividend_v1"


def migrate_lot_dividend(conn) -> int:
    """批次累计分红列上线后的一次性重放，为存量批次补齐 `received_div_cents`。

    系统口径从公司行为重新累积分红，交割单口径从红利/红利税现金流按持股数摊回
    批次；成功后写 meta 标记。返回本次重放的账户数（已迁移过则返回 0）。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (MIGRATION_LOT_DIVIDEND,)
    ).fetchone()
    if row is not None:
        return 0
    accounts = Repository(conn).list_accounts()
    for item in accounts:
        LedgerService(conn, int(item["account_id"])).rebuild_all()
    with Repository(conn).lock():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (MIGRATION_LOT_DIVIDEND, date.today().isoformat()),
        )
        conn.commit()
    return len(accounts)


def migrate_realized_avg_cost(conn) -> int:
    """已实现盈亏切到「移动加权平均 + 分红抵扣」后的一次性重放。

    旧库里的 `realized_pnl_cents` 是 FIFO 批次结转的结果，与券商 APP 对不上；
    对所有账户重放一次即可按新口径重写，成功后在 meta 写入标记。
    返回本次重放的账户数（已迁移过则返回 0）。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (MIGRATION_REALIZED_AVG_COST,)
    ).fetchone()
    if row is not None:
        return 0
    accounts = Repository(conn).list_accounts()
    for item in accounts:
        LedgerService(conn, int(item["account_id"])).rebuild_all()
    with Repository(conn).lock():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (MIGRATION_REALIZED_AVG_COST, date.today().isoformat()),
        )
        conn.commit()
    return len(accounts)


def migrate_dividend_exempt(conn) -> int:
    """旧口径红利状态一次性迁移。

    旧实现一律把分红记为待缴，且「先同步公司行为、后写新批次」会让新批次
    永远错过在册期间的分红。这里对所有账户做一次全量重放：免税批次改记
    EXEMPT、待缴基数只保留真正待缴部分、漏记的批次红利于重放时补齐。

    成功后在 meta 表写入标记，之后启动不再重放；失败则不写标记，下次启动重试。
    返回本次重放的账户数（已迁移过则返回 0）。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (MIGRATION_DIVIDEND_EXEMPT,)
    ).fetchone()
    if row is not None:
        return 0
    accounts = Repository(conn).list_accounts()
    for item in accounts:
        LedgerService(conn, int(item["account_id"])).rebuild_all()
    with Repository(conn).lock():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (MIGRATION_DIVIDEND_EXEMPT, date.today().isoformat()),
        )
        conn.commit()
    return len(accounts)


def default_fee_config() -> FeeConfig:
    return DEFAULT_FEE_CONFIG
