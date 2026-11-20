"""长衡 LongLedger 自动化测试（可直接运行，兼容 pytest）。

覆盖范围：
1. 最低 5 元佣金保底 + 印花税卖出单边
2. FIFO 批次扣减
3. 跨月 / 跨年 / 超一年 红利税延迟扣缴
4. ETF 免印花税、免红利税
5. 送转股成本摊薄与除权市值平滑
6. 全链路整数「分」精度
7. 官方除权公式与守恒校验

运行：python tests/test_all.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DEFAULT_FEE_CONFIG, FeeConfig  # noqa: E402
from core.corporate_actions import (  # noqa: E402
    CorporateAction,
    ConservationError,
    apply_corporate_action,
    check_conservation,
    ex_rights_price,
)
from core.fees import (  # noqa: E402
    AssetType,
    SIDE_BUY,
    SIDE_SELL,
    calculate_fees,
    classify_symbol,
)
from core.money import (  # noqa: E402
    MILLS_PER_CENT,
    add_months,
    qty_price_to_cents,
    round_half_up,
    yuan_to_mills,
)
from core.tax import (  # noqa: E402
    BRACKET_ETF_EXEMPT,
    BRACKET_OVER_ONE_YEAR,
    BRACKET_WITHIN_ONE_MONTH,
    BRACKET_WITHIN_ONE_YEAR,
    holding_period_bracket,
)
from core.avg_cost import AverageCostPool  # noqa: E402
from core.trading import Portfolio  # noqa: E402

P10 = yuan_to_mills("10.00")
P12 = yuan_to_mills("12.00")
P15 = yuan_to_mills("15.00")


class TestMinimumCommission(unittest.TestCase):
    """坑 3：最低 5 元佣金保底，小额建仓不得失真。"""

    def test_small_buy_hits_minimum(self) -> None:
        fees = calculate_fees(SIDE_BUY, 100, P10, "600000")
        # 成交额 1000 元，万 2.5 佣金仅 0.25 元 -> 必须保底 5 元
        self.assertEqual(fees.commission_cents, 500)
        self.assertEqual(fees.commission_cents, DEFAULT_FEE_CONFIG.min_commission_cents)

    def test_large_trade_uses_rate(self) -> None:
        fees = calculate_fees(SIDE_BUY, 20000, yuan_to_mills("20.00"), "600000")
        # 40 万元 × 万 2.5 = 100 元，高于保底
        self.assertEqual(fees.commission_cents, 10_000)

    def test_stamp_tax_sell_only_for_stock(self) -> None:
        buy = calculate_fees(SIDE_BUY, 1000, P10, "600000")
        sell = calculate_fees(SIDE_SELL, 1000, P10, "600000")
        self.assertEqual(buy.stamp_tax_cents, 0)
        self.assertEqual(sell.stamp_tax_cents, 500)  # 1 万元 × 0.05% = 5 元
        self.assertEqual(buy.commission_cents, sell.commission_cents)

    def test_portfolio_cash_matches_ledger(self) -> None:
        pf = Portfolio(initial_cash_cents=1_000_000)
        pf.buy("600000", date(2024, 1, 2), 100, P10)
        pf.sell("600000", date(2024, 1, 3), 100, P10)
        self.assertEqual(pf.cash_cents, sum(entry.amount_cents for entry in pf.ledger))
        self.assertEqual(pf.position_qty("600000"), 0)


class TestFifo(unittest.TestCase):
    """核心：卖出必须按买入时间先后扣减，且成本逐批结转。"""

    def setUp(self) -> None:
        self.pf = Portfolio(initial_cash_cents=10_000_000)
        self.pf.buy("600519", date(2023, 1, 10), 1000, P10)
        self.pf.buy("600519", date(2024, 7, 20), 500, P12)

    def test_lot_order(self) -> None:
        book = self.pf.book("600519")
        self.assertEqual([lot.lot_id for lot in book.fifo_order()], [1, 2])

    def test_sell_consumes_oldest_first(self) -> None:
        trade = self.pf.sell("600519", date(2024, 8, 5), 1200, P15)
        self.assertEqual(len(trade and self.pf.consumptions), 2)

        first, second = [item[1] for item in self.pf.consumptions]
        self.assertEqual((first.lot_id, first.qty), (1, 1000))
        self.assertEqual((second.lot_id, second.qty), (2, 200))

        # 批次 1 全额结转：1,000,000 + 佣金 500 + 过户费 10
        self.assertEqual(first.cost_released_cents, 1_000_510)
        # 批次 2 按比例结转：600,506 × 200 / 500 = 240,202.4 -> 240,202
        self.assertEqual(second.cost_released_cents, 240_202)

        book = self.pf.book("600519")
        self.assertEqual(book.total_qty, 300)
        self.assertEqual(book.lots[0].qty_remaining, 0)
        self.assertEqual(book.lots[1].qty_remaining, 300)
        self.assertEqual(book.cost_remaining_cents, 600_506 - 240_202)

    def test_reject_oversell(self) -> None:
        with self.assertRaises(ValueError):
            self.pf.sell("600519", date(2024, 8, 5), 1501, P15)

    def test_realized_pnl_closes_to_math(self) -> None:
        trade = self.pf.sell("600519", date(2024, 8, 5), 1200, P15)
        gross = qty_price_to_cents(1200, P15)
        fees = trade.fee_total_cents
        expected = gross - fees - sum(item[1].cost_released_cents for item in self.pf.consumptions)
        self.assertEqual(trade.realized_pnl_cents, expected)


class TestDividendTax(unittest.TestCase):
    """坑 2：卖出时按 FIFO 批次持股期限补扣红利税。"""

    def _build(self) -> Portfolio:
        pf = Portfolio(initial_cash_cents=100_000_000)
        pf.buy("600036", date(2023, 6, 1), 1000, P10)    # 持股 14 个月 -> 免税
        pf.buy("600036", date(2024, 7, 20), 500, P12)    # 持股 16 天 -> 20%
        action = CorporateAction(
            symbol="600036",
            ex_date=date(2024, 7, 25),
            cash_per_share_mills=yuan_to_mills("0.50"),
        )
        apply_corporate_action(pf, action, prev_close_mills=P12)
        return pf

    def test_ex_date_credits_full_cash_without_withholding(self) -> None:
        pf = self._build()
        # 除息日全额到账：(1000 + 500) × 0.5 元 = 750 元 = 75,000 分
        total_dividend = sum(record.gross_cents for record in pf.dividends)
        self.assertEqual(total_dividend, 75_000)
        # 除权日已持股满 1 年的批次当场免税，只有未满 1 年的批次进入待缴
        first, second = pf.dividends
        self.assertEqual((first.status, first.remaining_gross_cents), ("EXEMPT", 0))
        self.assertEqual((second.status, second.remaining_gross_cents), ("PENDING", 25_000))
        self.assertTrue(all(record.tax_cents == 0 for record in pf.dividends))

    def test_sell_withholds_by_lot_holding_period(self) -> None:
        pf = self._build()
        trade = pf.sell("600036", date(2024, 8, 5), 1200, P15)

        first, second = [item[1] for item in pf.consumptions]
        # 批次 1：持股超 1 年，免税
        self.assertEqual(first.tax_bracket, BRACKET_OVER_ONE_YEAR)
        self.assertEqual(first.dividend_tax_cents, 0)
        # 批次 2：16 天，20% × 100 元（200 股 × 0.5）= 20 元
        self.assertEqual(second.tax_bracket, BRACKET_WITHIN_ONE_MONTH)
        self.assertEqual(second.tax_rate, Decimal("0.20"))
        self.assertEqual(second.dividend_gross_cents, 10_000)
        self.assertEqual(second.dividend_tax_cents, 2_000)
        self.assertEqual(trade.dividend_tax_cents, 2_000)

        # 税额确实从卖出资金中扣除
        tax_entries = [e for e in pf.ledger if e.kind == "DIVIDEND_TAX"]
        self.assertEqual(len(tax_entries), 1)
        self.assertEqual(tax_entries[0].amount_cents, -2_000)

    def test_bracket_boundaries(self) -> None:
        # 对月对日：1 个月含当日
        self.assertEqual(
            holding_period_bracket(date(2024, 1, 31), date(2024, 2, 29))[1],
            BRACKET_WITHIN_ONE_MONTH,
        )
        self.assertEqual(
            holding_period_bracket(date(2024, 1, 31), date(2024, 3, 1))[1],
            BRACKET_WITHIN_ONE_YEAR,
        )
        # 满 1 年含当日按 10%，次日才免税
        self.assertEqual(
            holding_period_bracket(date(2024, 1, 31), date(2025, 1, 31))[1],
            BRACKET_WITHIN_ONE_YEAR,
        )
        self.assertEqual(
            holding_period_bracket(date(2024, 1, 31), date(2025, 2, 1))[1],
            BRACKET_OVER_ONE_YEAR,
        )

    def test_partial_sell_prorates_pending_dividend(self) -> None:
        pf = self._build()
        pf.sell("600036", date(2024, 8, 5), 500, P15)
        # 批次 1 除权日已满 1 年，红利免税不待缴；剩余待缴只有批次 2 的 25,000 分
        remaining = sum(record.remaining_gross_cents for record in pf.dividends)
        self.assertEqual(remaining, 25_000)

    def test_add_months_month_end(self) -> None:
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(add_months(date(2023, 1, 31), 1), date(2023, 2, 28))
        self.assertEqual(add_months(date(2024, 2, 29), 12), date(2025, 2, 28))


class TestEtfRules(unittest.TestCase):
    """ETF 专属：卖出免印花税、现金分红免红利税。"""

    def test_symbol_classification(self) -> None:
        self.assertEqual(classify_symbol("510300"), AssetType.ETF)
        self.assertEqual(classify_symbol("159915"), AssetType.ETF)
        self.assertEqual(classify_symbol("588000"), AssetType.ETF)
        self.assertEqual(classify_symbol("600519"), AssetType.STOCK)
        self.assertEqual(classify_symbol("000001"), AssetType.STOCK)

    def test_etf_stamp_tax_exempt_on_sell(self) -> None:
        etf_sell = calculate_fees(SIDE_SELL, 10_000, P10, "510300")
        stock_sell = calculate_fees(SIDE_SELL, 10_000, P10, "600000")
        self.assertEqual(etf_sell.stamp_tax_cents, 0)
        self.assertEqual(stock_sell.stamp_tax_cents, 5000)  # 10 万元 × 0.05% = 50 元
        # 佣金保底规则对 ETF 同样生效
        self.assertEqual(etf_sell.commission_cents, 2500)

    def test_etf_dividend_tax_exempt_even_within_one_month(self) -> None:
        pf = Portfolio(initial_cash_cents=20_000_000)
        pf.buy("510300", date(2024, 7, 1), 10_000, P10)
        action = CorporateAction(
            symbol="510300", ex_date=date(2024, 7, 10), cash_per_share_mills=yuan_to_mills("0.80")
        )
        apply_corporate_action(pf, action, prev_close_mills=P10)
        trade = pf.sell("510300", date(2024, 7, 15), 10_000, P10)

        self.assertEqual(trade.dividend_tax_cents, 0)
        consumption = pf.consumptions[0][1]
        self.assertEqual(consumption.tax_bracket, BRACKET_ETF_EXEMPT)
        # ETF 分红在除权日即记免税，不进入待缴基数
        self.assertEqual(consumption.dividend_gross_cents, 0)
        self.assertTrue(all(record.status == "EXEMPT" for record in pf.dividends))


class TestBonusShares(unittest.TestCase):
    """坑 4 + 坑 1：送转摊薄成本，且除权日市值不断裂。"""

    def test_official_ex_rights_formula(self) -> None:
        # (10.00 - 0.50) / (1 + 0.3) = 7.3077 -> 7.308
        self.assertEqual(ex_rights_price(10_000, 500, Decimal("0.3")), 7308)
        # 纯送转：(10.00) / (1 + 0.5) = 6.6667 -> 6.667
        self.assertEqual(ex_rights_price(10_000, 0, Decimal("0.5")), 6667)

    def test_cost_diluted_after_bonus(self) -> None:
        pf = Portfolio(initial_cash_cents=10_000_000)
        trade = pf.buy("600000", date(2024, 5, 10), 1000, P10)
        cost = -trade.net_cash_cents  # 1,000,510 分
        unit_before = pf.book("600000").lots[0].unit_cost_mills

        action = CorporateAction(symbol="600000", ex_date=date(2024, 6, 20), bonus_ratio=Decimal("0.5"))
        apply_corporate_action(pf, action, prev_close_mills=P10)

        lot = pf.book("600000").lots[0]
        self.assertEqual(lot.qty_remaining, 1500)
        self.assertEqual(lot.cost_remaining_cents, cost)  # 成本总额不变
        self.assertEqual(lot.unit_cost_mills, round_half_up(Decimal(cost).scaleb(1) / Decimal(1500)))
        self.assertLess(lot.unit_cost_mills, unit_before)

    def test_market_value_smooth_on_ex_date(self) -> None:
        pf = Portfolio(initial_cash_cents=10_000_000)
        pf.buy("600000", date(2024, 5, 10), 1000, P10)
        asset_before = pf.total_asset_cents({"600000": P10})

        action = CorporateAction(
            symbol="600000",
            ex_date=date(2024, 6, 20),
            cash_per_share_mills=yuan_to_mills("0.50"),
            bonus_ratio=Decimal("0.3"),
        )
        result = apply_corporate_action(pf, action, prev_close_mills=P10)

        dry = {
            "600000": result.ex_price_mills  # 按除权参考价重估
        }
        cash_before = pf.cash_cents - result.dividend_gross_cents - result.residue_cents
        asset_after = pf.total_asset_cents(dry)

        # 除权日总资产不得暴跌：允许毫级取整余量
        self.assertLessEqual(abs(asset_after - asset_before), result.allowance_cents)
        self.assertLess(result.allowance_cents, 200)
        _ = cash_before

    def test_conservation_guard_blocks_broken_action(self) -> None:
        # 合法除息：10 元派 5 元，除权价 5.00，守恒
        self.assertEqual(ex_rights_price(10_000, yuan_to_mills("5.00"), Decimal("0")), 5000)

        # 直接校验守恒器：股数被少记 -> 必须报错
        with self.assertRaises(ConservationError):
            check_conservation("600000", 1_000_000, 1000, 5000, 0, 0)

        # 正常情况通过
        post, drift, allowance = check_conservation("600000", 1_000_000, 1000, 10_000, 0, 0)
        self.assertEqual(post, 1_000_000)
        self.assertEqual(drift, 0)
        self.assertEqual(allowance, 52)

        # 分红超过前收盘价时，除权公式必须拒绝
        with self.assertRaises(ValueError):
            ex_rights_price(10_000, yuan_to_mills("20.00"), Decimal("0"))


class TestIntegerPrecision(unittest.TestCase):
    """金额精度防坑：所有资金字段必须是 int，绝不出现 float。"""

    def test_all_money_fields_are_int(self) -> None:
        pf = Portfolio(initial_cash_cents=10_000_000)
        buy = pf.buy("600519", date(2024, 1, 2), 100, P10)
        for value in (
            pf.cash_cents,
            buy.gross_cents,
            buy.commission_cents,
            buy.stamp_tax_cents,
            buy.transfer_fee_cents,
            buy.net_cash_cents,
            buy.realized_pnl_cents,
        ):
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, bool)

    def test_no_float_in_division(self) -> None:
        pf = Portfolio(initial_cash_cents=10_000_000)
        pf.buy("600519", date(2024, 1, 2), 333, yuan_to_mills("13.37"))
        lot = pf.book("600519").lots[0]
        self.assertIsInstance(lot.unit_cost_mills, int)
        self.assertIsInstance(lot.cost_remaining_cents, int)
        # 全部账本条目为整数分且合计等于现金
        self.assertEqual(pf.cash_cents, sum(entry.amount_cents for entry in pf.ledger))

    def test_price_with_three_decimals(self) -> None:
        # ETF 报价 3.587 元，毫级精度不丢
        self.assertEqual(yuan_to_mills("3.587"), 3587)
        self.assertEqual(qty_price_to_cents(1000, 3587), 358_700)


class TestFeeConfigAndThreading(unittest.TestCase):
    """线程安全修复 + 自定义费率参数持久化。"""

    def _temp_db(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        return tmp, Path(tmp) / "longledger.db"

    def test_connection_usable_across_threads(self) -> None:
        """复现用户场景：连接在主线程创建，服务在另一个线程读写。"""
        import shutil
        import threading

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)  # 主线程创建连接
            result: dict[str, int] = {}
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    service = LedgerService(conn)
                    service.buy("600000", date(2024, 1, 2), 100, "10.00")
                    result["qty"] = service.portfolio.position_qty("600000")
                    result["rows"] = conn.execute(
                        "SELECT COUNT(*) AS c FROM fee_config"
                    ).fetchone()["c"]
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(result["qty"], 100)
            self.assertEqual(result["rows"], 1)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_fee_config_persisted_and_applied(self) -> None:
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)
            custom = FeeConfig(
                commission_rate=Decimal("0.001"),      # 千分之一
                min_commission_cents=0,                # 关闭 5 元保底
                stamp_tax_rate=Decimal("0.001"),       # 千分之一，仅卖出
                transfer_fee_rate=Decimal("0.0001"),
                etf_stamp_tax_exempt=False,            # 关闭 ETF 免印花税
                etf_transfer_fee_exempt=False,
            )
            service.update_fee_config(custom)

            # 重新加载：参数必须从数据库读回
            reloaded = LedgerService(conn).current_fee_config()
            self.assertEqual(reloaded.commission_rate, Decimal("0.001"))
            self.assertEqual(reloaded.min_commission_cents, 0)
            self.assertEqual(reloaded.stamp_tax_rate, Decimal("0.001"))
            self.assertEqual(reloaded.transfer_fee_rate, Decimal("0.0001"))
            self.assertFalse(reloaded.etf_stamp_tax_exempt)
            self.assertFalse(reloaded.etf_transfer_fee_exempt)

            # 金额字段在库中必须为 INTEGER
            kind = conn.execute(
                "SELECT typeof(min_commission_cents) AS t FROM fee_config WHERE config_id = 1"
            ).fetchone()["t"]
            self.assertEqual(kind, "integer")

            # 小额买入不再触发 5 元保底：1000 元 × 0.1% = 1 元 = 100 分
            trade = service.buy("600000", date(2024, 1, 2), 100, "10.00")
            self.assertEqual(trade.commission_cents, 100)
            self.assertEqual(trade.transfer_fee_cents, 10)

            # ETF 免印花税开关关闭后，卖出必须计印花税
            service.buy("510300", date(2024, 1, 3), 1000, "4.000")
            etf_sell = service.sell("510300", date(2024, 1, 4), 1000, "4.500")
            self.assertEqual(etf_sell.stamp_tax_cents, 450)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestHoldingsAndAccount(unittest.TestCase):
    """持仓汇总聚合、初始本金调整与出入金流水。"""

    def _temp_db(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        return tmp, Path(tmp) / "longledger.db"

    def test_holdings_summary_aggregates_by_symbol(self) -> None:
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)
            service.buy("600000", date(2024, 1, 2), 100, "10.00")
            service.buy("600000", date(2024, 1, 5), 200, "12.00")
            service.set_close("600000", date(2024, 1, 8), "11.00")

            rows = service.holdings_summary(date(2024, 1, 8))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            cost = service.portfolio.book("600000").cost_remaining_cents
            avg_mills = round_half_up(Decimal(cost) * MILLS_PER_CENT / Decimal(300))

            self.assertEqual(row["代码"], "600000")
            self.assertEqual(row["持仓股数"], 300)
            self.assertEqual(row["原始成本(元)"], round(cost / 100, 2))
            self.assertEqual(row["原始成本(元/股)"], round(avg_mills / 1000, 3))
            # 无分红时摊薄成本与原始成本一致
            self.assertEqual(row["摊薄成本(元/股)"], round(avg_mills / 1000, 3))
            self.assertEqual(row["累计现金分红(元)"], 0.0)
            self.assertEqual(row["市值(元)"], 3300.0)          # 300 × 11.00
            self.assertEqual(row["持仓盈亏(元)"], round((330_000 - cost) / 100, 2))
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_broker_cost_resets_after_full_close(self) -> None:
        """整仓清空后重新开仓：上一轮已实现盈亏不再摊进新一轮成本价。"""
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)
            service.buy("513010", date(2024, 1, 2), 1000, "0.718")
            service.sell("513010", date(2024, 3, 1), 1000, "0.781")
            service.buy("513010", date(2024, 4, 1), 1000, "0.800")
            service.set_close("513010", date(2024, 4, 2), "0.900")

            row = service.holdings_summary(date(2024, 4, 2))[0]
            # 新一轮只有这一笔买入：0.800 + 5 元佣金 → 0.805/股
            self.assertEqual(row["摊薄成本(元/股)"], 0.805)
            self.assertEqual(row["持仓盈亏(元)"], 95.0)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_initial_capital_updates_basis_and_locks_after_trade(self) -> None:
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)
            self.assertEqual(service.initial_capital_cents(), 10_000_000)

            service.set_initial_capital(5_000_000)
            self.assertEqual(service.portfolio.cash_cents, 5_000_000)
            self.assertEqual(service.initial_capital_cents(), 5_000_000)
            # 期初入金流水就地改写，不新增脏流水
            row = conn.execute(
                "SELECT COUNT(*) AS c, SUM(amount_cents) AS s FROM cash_ledger"
            ).fetchone()
            self.assertEqual(row["c"], 1)
            self.assertEqual(row["s"], 5_000_000)

            # 重载后基准一致
            reloaded = LedgerService(conn)
            self.assertEqual(reloaded.portfolio.cash_cents, 5_000_000)
            self.assertEqual(reloaded.portfolio.initial_cash_cents, 5_000_000)

            service.buy("600000", date(2024, 1, 2), 100, "10.00")
            cash_after_buy = service.portfolio.cash_cents
            entries_after_buy = len(service.portfolio.ledger)
            # 已有交易流水：只改净值基准，现金与历史流水必须原封不动
            service.set_initial_capital(1_000_000)
            self.assertEqual(service.initial_capital_cents(), 1_000_000)
            self.assertEqual(service.portfolio.initial_cash_cents, 1_000_000)
            self.assertEqual(service.portfolio.cash_cents, cash_after_buy)
            self.assertEqual(len(service.portfolio.ledger), entries_after_buy)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_deposit_and_withdraw_append_ledger(self) -> None:
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)

            service.deposit(500_000)
            self.assertEqual(service.portfolio.cash_cents, 10_500_000)
            service.withdraw(200_000)
            self.assertEqual(service.portfolio.cash_cents, 10_300_000)

            with self.assertRaises(ValueError):
                service.withdraw(99_000_000)
            self.assertEqual(service.portfolio.cash_cents, 10_300_000)

            reloaded = LedgerService(conn)
            self.assertEqual(reloaded.portfolio.cash_cents, 10_300_000)
            kinds = [entry.kind for entry in reloaded.portfolio.ledger]
            self.assertEqual(kinds.count("DEPOSIT"), 2)   # 期初 + 本次入金
            self.assertEqual(kinds.count("WITHDRAW"), 1)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestMultiAccount(unittest.TestCase):
    """多账户：新建、数据隔离、级联删除与最后一个账户保护。"""

    def _temp_conn(self):
        import shutil
        import tempfile

        from data.db import open_database

        tmp = tempfile.mkdtemp()
        conn = open_database(Path(tmp) / "longledger.db")
        return tmp, conn, shutil

    def test_account_isolation_and_cascade_delete(self) -> None:
        from services.accounts import AccountService
        from services.ledger import LedgerService

        tmp, conn, shutil = self._temp_conn()
        try:
            accounts = AccountService(conn)
            first_id = accounts.list_accounts()[0]["account_id"]

            second_id = accounts.create("第二个账户", 5_000_000)
            second = LedgerService(conn, second_id)
            self.assertEqual(second.portfolio.cash_cents, 5_000_000)

            first = LedgerService(conn, first_id)
            first.buy("600000", date(2024, 1, 2), 100, "10.00")
            second.buy("600000", date(2024, 1, 3), 200, "10.00")
            second.buy("510300", date(2024, 1, 3), 1000, "4.000")

            # 同名账户不允许
            with self.assertRaises(ValueError):
                accounts.create("第二个账户", 1_000_000)

            self.assertEqual(first.portfolio.position_qty("600000"), 100)
            self.assertEqual(second.portfolio.position_qty("600000"), 200)
            self.assertEqual(second.portfolio.position_qty("510300"), 1000)
            self.assertEqual(len(first.repo.list_trades()), 1)
            self.assertEqual(len(second.repo.list_trades()), 2)
            self.assertEqual(len(second.portfolio.ledger), 1 + 2)  # 期初入金 + 两笔买入

            # 删除第二个账户：其流水级联清空，第一个账户完全不受影响
            accounts.delete(second_id)
            self.assertEqual(accounts.repo.count_accounts(), 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM trades WHERE account_id = ?", (second_id,)
                ).fetchone()["c"],
                0,
            )
            self.assertEqual(first.repo.account_cash_cents(first_id) > 0, True)
            self.assertEqual(len(LedgerService(conn, first_id).portfolio.trades), 1)

            # 最后一个账户不允许删除
            with self.assertRaises(ValueError):
                accounts.delete(first_id)
        finally:
            conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestSchemaMigration(unittest.TestCase):
    """重复打开数据库必须幂等：不重复迁移、不丢数据、外键完整。"""

    @unittest.skipUnless(
        Path(__file__).resolve().parents[1].joinpath("longledger.db").exists(),
        "无既有数据库，跳过",
    )
    def test_reopen_keeps_counts_and_integrity(self) -> None:
        import shutil
        import sqlite3
        import tempfile

        from data.db import open_database

        real_db = Path(__file__).resolve().parents[1] / "longledger.db"
        tmp = tempfile.mkdtemp()
        copy_path = Path(tmp) / "copy.db"
        try:
            src = sqlite3.connect(str(real_db))
            dst = sqlite3.connect(str(copy_path))
            src.backup(dst)
            dst.close()
            src.close()

            def counts(conn) -> dict[str, int]:
                return {
                    "trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                    "lots": conn.execute("SELECT COUNT(*) FROM trade_lots").fetchone()[0],
                    "ledger": conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0],
                    "snapshots": conn.execute(
                        "SELECT COUNT(*) FROM daily_snapshots"
                    ).fetchone()[0],
                }

            conn = open_database(copy_path)
            first = counts(conn)
            conn.close()
            conn = open_database(copy_path)
            second = counts(conn)
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            conn.close()

            self.assertEqual(first, second)
            self.assertEqual(version, "11")
            self.assertEqual(fk_errors, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestQuoteDownload(unittest.TestCase):
    """在线行情下载：代码识别、双源解析、批量入库与单标的失败隔离。"""

    def test_split_and_parse_symbols(self) -> None:
        from ingest.quotes_download import parse_symbol, split_symbols

        self.assertEqual(
            split_symbols("510300, 600000;\n159915 000001"), ["510300", "600000", "159915", "000001"]
        )
        self.assertEqual(parse_symbol("600000").market, 1)
        self.assertEqual(parse_symbol("600000.SH").market, 1)
        self.assertEqual(parse_symbol("sh600000").market, 1)
        self.assertEqual(parse_symbol("000001").market, 0)
        self.assertEqual(parse_symbol("sz000001").market, 0)
        self.assertEqual(parse_symbol("510300").market, 1)
        self.assertEqual(parse_symbol("159915").market, 0)
        self.assertEqual(parse_symbol("159915").exchange, "sz")

        for bad in ("", "60000", "abcdef", "830799"):
            with self.assertRaises(ValueError):
                parse_symbol(bad)

    def test_parse_eastmoney_dividend_payload(self) -> None:
        """东财分红报文：坏行跳过、每10股换算每股、结果按除权日排序。"""
        from ingest.dividends_download import parse_eastmoney_dividends

        payload = {
            "result": {
                "data": [
                    {  # 派息 10派3元 + 10送2转1
                        "EX_DIVIDEND_DATE": "2024-06-20",
                        "DIVIDEND_DATE": "2024-06-28",
                        "PRETAX_BONUS_RMB": "3.0",
                        "BONUS_IT_RATIO": "3.0",
                        "IT_RATIO": "1.0",
                    },
                    {"EX_DIVIDEND_DATE": None, "PRETAX_BONUS_RMB": "9.9"},  # 坏行
                    {  # 全零行应跳过
                        "EX_DIVIDEND_DATE": "2024-06-21",
                        "PRETAX_BONUS_RMB": "0",
                        "BONUS_IT_RATIO": "0",
                        "IT_RATIO": "0",
                    },
                    {  # 更早的除权日，验证排序
                        "EX_DIVIDEND_DATE": "2023-06-15",
                        "DIVIDEND_DATE": "2023-06-22",
                        "PRETAX_BONUS_RMB": "2.5",
                        "BONUS_IT_RATIO": "0",
                        "IT_RATIO": "0",
                    },
                ]
            }
        }
        events = parse_eastmoney_dividends(payload, "600000")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].ex_date, date(2023, 6, 15))
        self.assertEqual(events[0].cash_per_share_mills, 250)
        self.assertEqual(events[1].ex_date, date(2024, 6, 20))
        self.assertEqual(events[1].cash_per_share_mills, 300)
        self.assertEqual(events[1].bonus_ratio, Decimal("0.2"))
        self.assertEqual(events[1].transfer_ratio, Decimal("0.1"))
        self.assertEqual(events[1].pay_date, date(2024, 6, 28))

    def test_parse_eastmoney_and_tencent_payloads(self) -> None:
        from ingest.quotes_download import _parse_eastmoney, _parse_tencent

        em = {
            "data": {
                "code": "600000",
                "klines": [
                    "2024-01-02,9.90,10.00,10.10,9.80,1000,1000000,3.0,1.0,0.1,1.2",
                    "2024-01-03,10.00,10.20,10.30,9.90,1200,1200000,4.0,2.0,0.2,1.4",
                ],
            }
        }
        bars = _parse_eastmoney(em)
        self.assertEqual([b.close_mills for b in bars], [10_000, 10_200])
        self.assertEqual(bars[0].price_date, date(2024, 1, 2))

        tx = {
            "data": {
                "sh600000": {
                    "day": [
                        ["2024-01-02", "9.90", "10.005", "10.10", "9.80", "1000"],
                        ["2024-01-03", "10.00", "10.20", "10.30", "9.90", "1200"],
                    ]
                }
            }
        }
        bars = _parse_tencent(tx, "sh600000")
        self.assertEqual([b.close_mills for b in bars], [10_005, 10_200])

    def test_download_history_writes_quotes_and_isolates_failures(self) -> None:
        import shutil
        import tempfile

        import ingest.quotes_download as qd
        from data.db import open_database
        from data.repositories import Repository

        tmp = tempfile.mkdtemp()
        conn = None
        original = qd.fetch_daily_closes
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            repo = Repository(conn)

            def fake_fetch(raw, start, end, timeout=10):
                if raw == "bad":
                    raise qd.QuoteDownloadError("模拟失败")
                ref = qd.parse_symbol(raw)
                return ref, [qd.Bar(date(2024, 1, 2), 10_000), qd.Bar(date(2024, 1, 3), 10_200)]

            qd.fetch_daily_closes = fake_fetch
            results = qd.download_history(
                repo, ["600000", "bad", "600000"], date(2024, 1, 1), date(2024, 1, 5)
            )

            ok = [r for r in results if not r.error]
            failed = [r for r in results if r.error]
            self.assertEqual(len(ok), 1)
            self.assertEqual(ok[0].rows, 2)
            self.assertEqual(ok[0].first_date, date(2024, 1, 2))
            self.assertEqual(len(failed), 1)
            self.assertIn("模拟失败", failed[0].error)

            self.assertEqual(repo.latest_price("600000", date(2024, 1, 5)), 10_200)
            self.assertEqual(repo.latest_price("600000", date(2024, 1, 2)), 10_000)
            # 行情同步的增量起点：最新已下载日；从未下载过的标的返回 None
            self.assertEqual(repo.latest_quote_date("600000"), date(2024, 1, 3))
            self.assertIsNone(repo.latest_quote_date("510300"))

            with self.assertRaises(ValueError):
                qd.download_history(repo, ["600000"], date(2024, 2, 1), date(2024, 1, 1))
        finally:
            qd.fetch_daily_closes = original
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestQuotesService(unittest.TestCase):
    """行情服务：单日 OHLC、非交易日回退、错误不外抛。"""

    def setUp(self) -> None:
        import services.quotes_service as qs

        self.qs = qs
        self.original = qs.fetch_ohlc_bars
        self.bars = [
            qs_daily(date(2024, 1, 2), 9900, 10100, 9800, 10000),
            qs_daily(date(2024, 1, 3), 10000, 10300, 9900, 10200),
        ]

        def fake_fetch(raw_symbol, start, end, timeout=10):
            from ingest.quotes_download import parse_symbol

            ref = parse_symbol(raw_symbol)
            return ref, [bar for bar in self.bars if start <= bar.price_date <= end]

        self.fake = fake_fetch
        qs.fetch_ohlc_bars = fake_fetch

    def tearDown(self) -> None:
        self.qs.fetch_ohlc_bars = self.original

    def test_exact_trading_day_returns_all_prices(self) -> None:
        quote = self.qs.get_daily_ohlc("600000", date(2024, 1, 3))
        self.assertTrue(quote.ok)
        self.assertTrue(quote.is_exact_date)
        self.assertEqual(quote.price_date, date(2024, 1, 3))
        self.assertEqual(quote.prev_close_mills, 10_000)
        self.assertEqual(quote.open_mills, 10_000)
        self.assertEqual(quote.high_mills, 10_300)
        self.assertEqual(quote.low_mills, 9_900)
        self.assertEqual(quote.close_mills, 10_200)
        self.assertEqual(str(quote.close), "10.2")

    def test_non_trading_day_falls_back_and_marks_date(self) -> None:
        quote = self.qs.get_daily_ohlc("600000", date(2024, 1, 6))
        self.assertTrue(quote.ok)
        self.assertFalse(quote.is_exact_date)
        self.assertEqual(quote.price_date, date(2024, 1, 3))
        self.assertEqual(quote.close_mills, 10_200)
        payload = quote.as_dict()
        self.assertEqual(payload["price_date"], "2024-01-03")
        self.assertEqual(payload["requested_date"], "2024-01-06")
        self.assertEqual(payload["close_yuan"], "10.2")

    def test_non_trading_day_without_fallback_returns_error(self) -> None:
        quote = self.qs.get_daily_ohlc(
            "600000", date(2024, 1, 6), allow_previous_trading_day=False
        )
        self.assertFalse(quote.ok)
        self.assertIn("最近交易日", quote.error)
        self.assertEqual(quote.price_date, date(2024, 1, 3))

    def test_network_failure_and_bad_symbol_do_not_raise(self) -> None:
        from ingest.quotes_download import QuoteDownloadError

        def boom(raw_symbol, start, end, timeout=10):
            raise QuoteDownloadError("模拟网络中断")

        self.qs.fetch_ohlc_bars = boom
        quote = self.qs.get_daily_ohlc("600000", date(2024, 1, 3))
        self.assertFalse(quote.ok)
        self.assertIn("模拟网络中断", quote.error)

        self.qs.fetch_ohlc_bars = self.fake
        bad = self.qs.get_daily_ohlc("830799", date(2024, 1, 3))
        self.assertFalse(bad.ok)
        self.assertTrue(bad.error)

        no_data = self.qs.get_daily_ohlc("600000", date(2023, 1, 3))
        self.assertFalse(no_data.ok)
        self.assertIn("无可用", no_data.error)


class TestDividendSync(unittest.TestCase):
    """公司行为服务：自动同步、按在册批次过滤、幂等与手动录入兜底。"""

    def setUp(self) -> None:
        import shutil
        import tempfile

        import services.corporate_actions_service as cas
        from data.db import open_database
        from services.ledger import LedgerService

        self.cas = cas
        self.shutil = shutil
        self.tmp = tempfile.mkdtemp()
        self.conn = open_database(Path(self.tmp) / "longledger.db")
        self.ledger = LedgerService(self.conn)
        self.ledger.buy("600000", date(2024, 1, 2), 1000, "10.00")
        self.ledger.buy("510300", date(2024, 7, 1), 10_000, "4.000")

        self.original_fetch = cas.fetch_dividend_events
        self.original_prev = cas.get_prev_close_mills
        self.events = [
            cas_dividend(
                "600000", date(2024, 6, 20), 420, Decimal("0.2"), Decimal("0")
            )
        ]
        cas.fetch_dividend_events = lambda ticker, timeout=10: list(self.events)
        cas.get_prev_close_mills = lambda ticker, day, timeout=10: 10_000

    def tearDown(self) -> None:
        self.cas.fetch_dividend_events = self.original_fetch
        self.cas.get_prev_close_mills = self.original_prev
        self.conn.close()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_applies_dividend_and_bonus(self) -> None:
        service = self.cas.CorporateActionService(self.conn)
        result = service.fetch_and_sync_dividends("600000", until=date(2024, 12, 31))

        self.assertEqual((result.fetched, result.applied, result.skipped), (1, 1, 0))
        self.assertEqual(result.errors, [])

        book = service.ledger.portfolio.book("600000")
        lot = book.lots[0]
        self.assertEqual(lot.qty_remaining, 1200)          # 10 送 2
        self.assertEqual(lot.buy_price_mills, 10_000)      # 买入原价固定
        self.assertLess(lot.diluted_cost_mills, 10_000)    # 摊薄成本下降

        rows = service.repo.list_corporate_actions("600000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cash_per_share_mills"], 420)

        dividends = service.repo.dividend_records_for_lot("600000", lot.lot_id)
        self.assertEqual(len(dividends), 1)
        self.assertEqual(dividends[0]["gross_cents"], 42_000)   # 1000 × 0.42 元
        self.assertEqual(dividends[0]["status"], "PENDING")     # 除息日不扣税

    def test_sync_is_idempotent(self) -> None:
        service = self.cas.CorporateActionService(self.conn)
        service.fetch_and_sync_dividends("600000", until=date(2024, 12, 31))
        cash_after = service.ledger.portfolio.cash_cents

        again = service.fetch_and_sync_dividends("600000", until=date(2024, 12, 31))
        self.assertEqual(again.applied, 0)
        self.assertEqual(again.skipped, 1)
        self.assertEqual(service.ledger.portfolio.cash_cents, cash_after)
        self.assertEqual(len(service.repo.list_corporate_actions("600000")), 1)

    def test_events_before_purchase_are_skipped(self) -> None:
        self.events = [
            cas_dividend("600000", date(2023, 6, 20), 300, Decimal("0"), Decimal("0"))
        ]
        service = self.cas.CorporateActionService(self.conn)
        result = service.fetch_and_sync_dividends("600000", until=date(2024, 12, 31))
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(service.repo.list_corporate_actions("600000"), [])

    def test_fetch_failure_reported_not_raised(self) -> None:
        from ingest.dividends_download import DividendFetchError

        def boom(ticker, timeout=10):
            raise DividendFetchError("模拟接口不可用")

        self.cas.fetch_dividend_events = boom
        service = self.cas.CorporateActionService(self.conn)
        result = service.fetch_and_sync_dividends("600000")
        self.assertFalse(result.ok)
        self.assertIn("模拟接口不可用", result.errors[0])

    def test_etf_without_source_data_prompts_manual_entry(self) -> None:
        self.cas.fetch_dividend_events = lambda ticker, timeout=10: []
        service = self.cas.CorporateActionService(self.conn)
        result = service.fetch_and_sync_dividends("510300")
        self.assertEqual(result.fetched, 0)
        self.assertIn("手动录入", result.note)

    def test_manual_entry_remains_available(self) -> None:
        service = self.cas.CorporateActionService(self.conn)
        result = service.record_manual_dividend(
            "510300",
            date(2024, 7, 10),
            cash_per_share_yuan="0.800",
            prev_close_yuan="4.000",
        )
        self.assertEqual(result.dividend_gross_cents, 800_000)   # 10000 × 0.8 元
        self.assertEqual(result.qty_added, 0)
        rows = service.repo.list_corporate_actions("510300")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cash_per_share_mills"], 800)

    def test_manual_and_auto_do_not_double_apply(self) -> None:
        """手动录入（0.000 写法）与自动同步（0）必须识别为同一事件。"""
        self.events = [
            cas_dividend("600000", date(2024, 6, 20), 420, Decimal("0"), Decimal("0"))
        ]
        service = self.cas.CorporateActionService(self.conn)
        service.record_manual_dividend(
            "600000",
            date(2024, 6, 20),
            cash_per_share_yuan="0.420",
            bonus_ratio="0.000",
            prev_close_yuan="10.000",
        )
        dividend_before = service.ledger.portfolio.book("600000").lots[0].pending_div_cents

        result = service.fetch_and_sync_dividends("600000", until=date(2024, 12, 31))
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(
            service.ledger.portfolio.book("600000").lots[0].pending_div_cents,
            dividend_before,
        )


class TestTradeReplay(unittest.TestCase):
    """成交删改后的全量重放：批次、现金、红利与公司行为全部重建。"""

    def setUp(self) -> None:
        import shutil
        import tempfile

        from data.db import open_database
        from services.ledger import LedgerService

        self.shutil = shutil
        self.tmp = tempfile.mkdtemp()
        self.conn = open_database(Path(self.tmp) / "longledger.db")
        self.ledger = LedgerService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def _trade_ids(self) -> list[int]:
        return [trade.trade_id for trade in self.ledger.portfolio.trades]

    def test_delete_middle_buy_rebuilds_lots_and_cash(self) -> None:
        self.ledger.buy("600000", date(2024, 1, 2), 1000, "10.00")
        self.ledger.buy("600000", date(2024, 2, 1), 1000, "11.00")
        self.ledger.sell("600000", date(2024, 3, 1), 500, "12.00")
        _t1, t2, _t3 = self._trade_ids()

        self.ledger.delete_trades([t2])

        trades = self.ledger.portfolio.trades
        self.assertEqual([trade.side for trade in trades], ["BUY", "SELL"])
        book = self.ledger.portfolio.book("600000")
        self.assertEqual(len(book.lots), 1)
        lot = book.lots[0]
        self.assertEqual(lot.qty_opened, 1000)
        self.assertEqual(lot.qty_remaining, 500)   # FIFO 只从第一批扣
        # 删掉的买入占款回到现金：现金 = 期初 - 第一批成本 + 卖出净额
        sell = [t for t in trades if t.side == "SELL"][0]
        expected = 10_000_000 - lot.cost_cents + sell.net_cash_cents
        self.assertEqual(self.ledger.portfolio.cash_cents, expected)
        # 卖出已实现盈亏重算：500 股全部结转自 10 元批次
        self.assertEqual(sell.realized_pnl_cents, sell.gross_cents - sell.commission_cents
                         - sell.stamp_tax_cents - sell.transfer_fee_cents
                         - sell.dividend_tax_cents - lot.cost_cents // 2)

    def test_update_trade_qty_rebuilds_cost(self) -> None:
        self.ledger.buy("600000", date(2024, 1, 2), 1000, "10.00")
        (trade_id,) = self._trade_ids()

        self.ledger.update_trade(
            trade_id,
            symbol="600000",
            trade_date=date(2024, 1, 2),
            qty=2000,
            price_yuan="10.00",
        )

        book = self.ledger.portfolio.book("600000")
        self.assertEqual(book.total_qty, 2000)
        trade = self.ledger.portfolio.trades[0]
        self.assertEqual(trade.qty, 2000)
        self.assertEqual(trade.gross_cents, 2_000_000)   # 2000 股 × 10 元
        self.assertEqual(len(self.ledger.repo.list_trades(limit=10)), 1)

    def test_failed_replay_keeps_database_untouched(self) -> None:
        self.ledger.buy("600000", date(2024, 1, 2), 1000, "10.00")
        self.ledger.sell("600000", date(2024, 3, 1), 500, "12.00")
        t1, _t2 = self._trade_ids()
        cash_before = self.ledger.portfolio.cash_cents

        # 删掉买入后卖出数量不足，重放必须失败且数据库分毫不动
        with self.assertRaises(ValueError):
            self.ledger.delete_trades([t1])
        self.assertEqual(len(self.ledger.repo.list_trades(limit=10)), 2)
        self.assertEqual(self.ledger.portfolio.cash_cents, cash_before)

    def test_delete_sell_restores_pending_dividend(self) -> None:
        from services.corporate_actions_service import CorporateActionService

        self.ledger.buy("600000", date(2024, 1, 2), 1000, "10.00")
        service = CorporateActionService(self.conn)
        service.record_manual_dividend(
            "600000",
            date(2024, 6, 20),
            cash_per_share_yuan="0.42",
            bonus_ratio="0.2",
            transfer_ratio="0",
            pay_date=date(2024, 6, 28),
            prev_close_yuan="10.00",
        )
        self.ledger.sell("600000", date(2024, 7, 2), 600, "9.00")
        sell_id = self._trade_ids()[-1]

        self.ledger.delete_trades([sell_id])

        book = self.ledger.portfolio.book("600000")
        self.assertEqual(book.total_qty, 1200)               # 送转股数保留
        actions = self.ledger.repo.list_corporate_actions("600000")
        self.assertEqual(len(actions), 1)                    # 公司行为事件不丢
        dividends = self.ledger.repo.dividend_records_for_lot(
            "600000", book.lots[0].lot_id
        )
        self.assertEqual(len(dividends), 1)
        self.assertEqual(dividends[0]["status"], "PENDING")  # 卖出冲销，税回到待缴
        self.assertEqual(dividends[0]["remaining_gross_cents"], 42_000)

    def test_replay_keeps_manual_cash_entries(self) -> None:
        self.ledger.deposit(1_000_000, date(2024, 1, 5))
        self.ledger.buy("600000", date(2024, 1, 8), 1000, "10.00")
        self.ledger.withdraw(500_000, date(2024, 2, 5))
        (trade_id,) = self._trade_ids()
        cash_before = self.ledger.portfolio.cash_cents

        self.ledger.update_trade(
            trade_id,
            symbol="600000",
            trade_date=date(2024, 1, 8),
            qty=1000,
            price_yuan="10.00",
        )

        kinds = [
            (row["kind"], row["amount_cents"])
            for row in self.ledger.repo.list_cash_entries()
            if row["kind"] in ("DEPOSIT", "WITHDRAW") and row["entry_date"] != "1900-01-01"
        ]
        self.assertEqual(kinds, [("DEPOSIT", 1_000_000), ("WITHDRAW", -500_000)])
        self.assertEqual(self.ledger.portfolio.cash_cents, cash_before)


class TestSecuritiesService(unittest.TestCase):
    """证券名称缓存：接口补全、写库复用、失败容忍。"""

    def setUp(self) -> None:
        import shutil
        import tempfile

        import services.securities_service as ss
        from data.db import open_database

        self.ss = ss
        self.shutil = shutil
        self.tmp = tempfile.mkdtemp()
        self.conn = open_database(Path(self.tmp) / "longledger.db")
        self.original_fetch = ss.fetch_security_name

    def tearDown(self) -> None:
        self.ss.fetch_security_name = self.original_fetch
        self.conn.close()
        self.shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_names_caches_and_reuses(self) -> None:
        calls: list[str] = []

        def fake(symbol, timeout=8):
            calls.append(symbol)
            return f"名称{symbol}"

        self.ss.fetch_security_name = fake
        first = self.ss.ensure_names(self.conn, ["600519", "601919", "600519"])
        self.assertEqual(first, {"600519": "名称600519", "601919": "名称601919"})
        self.assertEqual(calls, ["600519", "601919"])   # 入参去重

        second = self.ss.ensure_names(self.conn, ["600519"])
        self.assertEqual(second, {"600519": "名称600519"})
        self.assertEqual(calls, ["600519", "601919"])   # 命中缓存不再请求
        self.assertEqual(self.ss.cached_names(self.conn)["601919"], "名称601919")

    def test_ensure_names_tolerates_failures(self) -> None:
        def boom(symbol, timeout=8):
            raise RuntimeError("网络不可用")

        self.ss.fetch_security_name = boom
        self.assertEqual(self.ss.ensure_names(self.conn, ["600519"]), {})

        self.ss.fetch_security_name = lambda symbol, timeout=8: None
        self.assertEqual(self.ss.ensure_names(self.conn, ["600519"]), {})


class TestLotDualCosts(unittest.TestCase):
    """批次双成本字段：买入原价固定、摊薄成本随送转变化，并可查公司行为明细。"""

    def test_portfolio_lot_keeps_buy_price_after_bonus(self) -> None:
        pf = Portfolio(initial_cash_cents=10_000_000)
        pf.buy("600000", date(2024, 5, 10), 1000, P10)
        lot = pf.book("600000").lots[0]
        self.assertEqual(lot.buy_price_mills, P10)
        # 摊薄成本含买入费用，故略高于成交价
        self.assertEqual(lot.diluted_cost_mills, 10_005)

        apply_corporate_action(
            pf,
            CorporateAction(symbol="600000", ex_date=date(2024, 6, 20), bonus_ratio=Decimal("0.5")),
            prev_close_mills=P10,
        )
        self.assertEqual(lot.buy_price_mills, P10)             # 原价不变
        self.assertEqual(lot.qty_remaining, 1500)
        self.assertLess(lot.diluted_cost_mills, lot.buy_price_mills)

    def test_dual_cost_and_dividend_pnl(self) -> None:
        """原始成本锁定；摊薄成本按券商保本价，盈亏含已实现。"""
        import shutil
        import tempfile

        from data.db import open_database
        from services.ledger import LedgerService

        tmp = tempfile.mkdtemp()
        conn = None
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            service = LedgerService(conn)
            service.buy("510300", date(2024, 1, 2), 1000, "10.00")
            service.apply_action(
                CorporateAction(
                    symbol="510300",
                    ex_date=date(2024, 6, 20),
                    cash_per_share_mills=yuan_to_mills("0.20"),
                ),
                "10.00",
            )
            lot = service.portfolio.book("510300").lots[0]
            self.assertEqual(lot.cost_cents, 1_000_500)      # 含 5.00 买入费用
            self.assertEqual(lot.received_div_cents, 20_000)  # ETF 分红免税，全额入批次
            self.assertEqual(lot.original_cost_mills, 10_005)  # 原始成本锁死
            self.assertEqual(lot.diluted_cost_mills, 9_805)    # 扣掉 0.20/股分红

            service.set_close("510300", date(2024, 6, 21), "11.00")
            row = service.holdings_summary(date(2024, 6, 21))[0]
            self.assertEqual(row["原始成本(元)"], 10_005.0)
            self.assertEqual(row["原始成本(元/股)"], 10.005)
            self.assertEqual(row["摊薄成本(元/股)"], 9.805)
            self.assertEqual(row["累计现金分红(元)"], 200.0)
            # 11,000 − 10,005 + 200 = 1,195
            self.assertEqual(row["持仓盈亏(元)"], 1_195.0)

            # 部分卖出：分红按剩余股数比例保留，摊薄成本不变
            service.sell("510300", date(2024, 7, 1), 400, "11.00")
            lot = service.portfolio.book("510300").lots[0]
            self.assertEqual(lot.qty_remaining, 600)
            self.assertEqual(lot.cost_remaining_cents, 600_300)
            self.assertEqual(lot.received_div_cents, 12_000)
            self.assertEqual(lot.original_cost_mills, 10_005)
            self.assertEqual(lot.diluted_cost_mills, 9_805)

            positions = service.positions()[0]
            self.assertEqual(positions["原始成本(元/股)"], 10.005)
            self.assertEqual(positions["批次摊薄成本(元/股)"], 9.805)
            self.assertEqual(positions["累计分红(元)"], 120.0)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_positions_and_details_survive_reload(self) -> None:
        import shutil
        import tempfile

        from data.db import open_database
        from services.ledger import LedgerService

        tmp = tempfile.mkdtemp()
        conn = None
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            service = LedgerService(conn)
            service.buy("600000", date(2024, 1, 2), 1000, "10.00")
            service.apply_action(
                CorporateAction(
                    symbol="600000",
                    ex_date=date(2024, 6, 20),
                    cash_per_share_mills=yuan_to_mills("0.42"),
                    bonus_ratio=Decimal("0.2"),
                ),
                "10.00",
            )

            reloaded = LedgerService(conn)
            lot = reloaded.portfolio.book("600000").lots[0]
            self.assertEqual(lot.buy_price_mills, 10_000)
            self.assertEqual(lot.qty_remaining, 1200)

            row = reloaded.positions()[0]
            self.assertEqual(row["买入原价(元/股)"], 10.0)
            self.assertAlmostEqual(row["批次摊薄成本(元/股)"], lot.diluted_cost_mills / 1000, places=3)

            details = reloaded.lot_corporate_actions("600000")
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["类型"], "分红+送转")
            self.assertEqual(details[0]["本批次红利(元)"], 420.0)
            # 红利状态按「今日」持股期限动态判定：买入于 2024-01-02，至今日已满 1 年，动态翻转为免税
            self.assertEqual(details[0]["红利状态"], "EXEMPT")
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


def qs_daily(day: date, open_mills: int, high_mills: int, low_mills: int, close_mills: int):
    from ingest.quotes_download import OhlcBar

    return OhlcBar(
        price_date=day,
        open_mills=open_mills,
        high_mills=high_mills,
        low_mills=low_mills,
        close_mills=close_mills,
    )


def cas_dividend(ticker, ex_date, cash_mills, bonus_ratio, transfer_ratio):
    from ingest.dividends_download import DividendEvent

    return DividendEvent(
        ticker=ticker,
        ex_date=ex_date,
        cash_per_share_mills=cash_mills,
        bonus_ratio=bonus_ratio,
        transfer_ratio=transfer_ratio,
    )


class TestPersistence(unittest.TestCase):
    """数据层闭环：落库 -> 重载后现金、批次、红利、快照必须一致。"""

    def test_round_trip(self) -> None:
        import shutil
        import tempfile

        from data.db import open_database
        from data.repositories import Repository
        from services.ledger import LedgerService
        from services.settlement import settle_day

        tmp = tempfile.mkdtemp()
        conn = None
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            service = LedgerService(conn)
            service.buy("600036", date(2023, 6, 1), 1000, "10.00")
            service.buy("600036", date(2024, 7, 20), 500, "12.00")

            action = CorporateAction(
                symbol="600036",
                ex_date=date(2024, 7, 25),
                cash_per_share_mills=yuan_to_mills("0.50"),
            )
            service.apply_action(action, "12.00")
            service.sell("600036", date(2024, 8, 5), 1200, "15.00")

            service.set_close("510300", date(2024, 8, 5), "4.000")
            snapshot = settle_day(service.repo, service.portfolio, date(2024, 8, 5))

            reloaded = Repository(conn).load_portfolio()
            self.assertEqual(reloaded.cash_cents, service.portfolio.cash_cents)
            self.assertEqual(reloaded.position_qty("600036"), 300)
            self.assertEqual(len(reloaded.trades), 3)
            self.assertEqual(len(reloaded.dividends), 2)
            self.assertEqual(
                sum(record.tax_cents for record in reloaded.dividends),
                service.portfolio.trades[-1].dividend_tax_cents,
            )

            snapshots = Repository(conn).list_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].nav_scaled, snapshot.nav_scaled)
            self.assertEqual(
                reloaded.cash_cents, sum(entry.amount_cents for entry in reloaded.ledger)
            )
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestAverageCostPool(unittest.TestCase):
    """券商口径的移动加权平均成本池：分红摊薄、送转加股、清仓闭合。"""

    def test_release_uses_moving_weighted_average(self) -> None:
        pool = AverageCostPool()
        pool.buy("600519", 1000, 1_000_500)   # 10.005 元/股
        pool.buy("600519", 500, 600_500)      # 12.01 元/股
        self.assertEqual(pool.qty("600519"), 1500)
        self.assertEqual(pool.cost_cents("600519"), 1_601_000)

        released = pool.release("600519", 1200)
        # 1601000 × 1200 / 1500 = 1,280,800，与 FIFO 挑最贵/最便宜批次无关
        self.assertEqual(released, 1_280_800)
        self.assertEqual(pool.qty("600519"), 300)
        self.assertEqual(pool.cost_cents("600519"), 320_200)

    def test_dividend_dilutes_cost_and_tax_adds_back(self) -> None:
        pool = AverageCostPool()
        pool.buy("600000", 1000, 1_000_500)
        pool.reduce_cost("600000", 42_000)    # 每股 0.42 元现金分红
        self.assertEqual(pool.cost_cents("600000"), 958_500)
        pool.reduce_cost("600000", -8_400)    # 股息红利税是分红的追回
        self.assertEqual(pool.cost_cents("600000"), 966_900)

    def test_bonus_shares_only_increase_quantity(self) -> None:
        pool = AverageCostPool()
        pool.buy("600000", 1000, 1_000_500)
        pool.add_shares("600000", 500)        # 10 送 5，成本不变
        self.assertEqual(pool.qty("600000"), 1500)
        self.assertEqual(pool.cost_cents("600000"), 1_000_500)
        self.assertEqual(pool.release("600000", 1500), 1_000_500)

    def test_clearance_releases_all_cost_without_residue(self) -> None:
        pool = AverageCostPool()
        pool.buy("000001", 300, 1_000_000)
        first = pool.release("000001", 100)   # 333,333.33 -> 333,333
        second = pool.release("000001", 200)
        self.assertEqual(first, 333_333)
        self.assertEqual(second, 666_667)
        self.assertEqual(pool.qty("000001"), 0)
        self.assertEqual(pool.cost_cents("000001"), 0)
        self.assertEqual(pool.release("000001", 100), 0)

    def test_reduce_cost_ignored_when_flat(self) -> None:
        pool = AverageCostPool()
        pool.reduce_cost("000001", 50_000)
        self.assertEqual(pool.cost_cents("000001"), 0)

    def test_replay_reports_weighted_average_realized_pnl(self) -> None:
        """两批不同价格买入 + 分红摊薄：已实现盈亏必须按均价结转，而非 FIFO 挑批次。"""
        import shutil
        import tempfile

        from data.db import open_database
        from services.corporate_actions_service import CorporateActionService
        from services.ledger import LedgerService

        tmp = tempfile.mkdtemp()
        conn = None
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            ledger = LedgerService(conn)
            ledger.buy("600000", date(2023, 1, 3), 1000, "10.00")
            ledger.buy("600000", date(2023, 6, 1), 1000, "20.00")
            CorporateActionService(conn).record_manual_dividend(
                "600000",
                date(2024, 6, 20),
                cash_per_share_yuan="0.42",
                bonus_ratio="0",
                transfer_ratio="0",
                pay_date=date(2024, 6, 28),
                prev_close_yuan="20.00",
            )
            sell = ledger.sell("600000", date(2025, 7, 2), 1000, "15.00")

            buys = [t for t in ledger.portfolio.trades if t.side == "BUY"]
            pool_cost = -sum(t.net_cash_cents for t in buys) - 84_000
            expected = sell.net_cash_cents - round_half_up(Decimal(pool_cost) / 2)
            self.assertEqual(sell.realized_pnl_cents, expected)
            self.assertEqual(sell.dividend_tax_cents, 0)   # 持有超一年免税

            fifo_cost = abs(buys[0].net_cash_cents)
            self.assertNotEqual(sell.realized_pnl_cents, sell.net_cash_cents - fifo_cost)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


    def test_migration_rewrites_legacy_fifo_realized_pnl(self) -> None:
        """旧库里的 FIFO 盈亏在启动迁移时被重放成加权摊薄口径。"""
        import shutil
        import tempfile

        from data.db import open_database
        from services.ledger import LedgerService, migrate_realized_avg_cost

        tmp = tempfile.mkdtemp()
        conn = None
        try:
            conn = open_database(Path(tmp) / "longledger.db")
            ledger = LedgerService(conn)
            ledger.buy("600000", date(2023, 1, 3), 1000, "10.00")
            ledger.buy("600000", date(2023, 6, 1), 1000, "20.00")
            expected = ledger.sell("600000", date(2025, 7, 2), 1000, "15.00")

            # 模拟旧版本写下的 FIFO 结果
            conn.execute("UPDATE trades SET realized_pnl_cents = 498225 WHERE side = 'SELL'")
            conn.commit()

            self.assertEqual(migrate_realized_avg_cost(conn), 1)
            row = conn.execute(
                "SELECT realized_pnl_cents FROM trades WHERE side = 'SELL'"
            ).fetchone()
            self.assertEqual(row[0], expected.realized_pnl_cents)
            self.assertEqual(migrate_realized_avg_cost(conn), 0)   # 只跑一次
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestTradeNotes(unittest.TestCase):
    """交易复盘字段：可选填写、随全量重放搬运、不参与账务计算。"""

    def _temp_db(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        return tmp, Path(tmp) / "longledger.db"

    def test_notes_are_optional_and_survive_replay(self) -> None:
        import shutil

        from data.db import open_database
        from services.ledger import LedgerService

        tmp, db_path = self._temp_db()
        conn = None
        try:
            conn = open_database(db_path)
            service = LedgerService(conn)
            first = service.buy(
                "600000",
                date(2024, 1, 2),
                100,
                "10.00",
                notes={
                    "trade_logic": "低估分批建仓",
                    "target_price_mills": 13_500,
                    "plan_type": "计划内",
                    "hold_period": "长期",
                },
            )
            self.assertEqual(first.trade_logic, "低估分批建仓")
            self.assertEqual(first.target_price_mills, 13_500)

            # 不填复盘也必须能正常入账
            second = service.buy("600000", date(2024, 1, 5), 100, "11.00")
            self.assertEqual(second.trade_logic, "")

            # 第二笔触发全量重放：第一笔的复盘不能丢
            buys = [t for t in service.portfolio.trades if t.side == "BUY"]
            self.assertEqual([t.trade_logic for t in buys], ["低估分批建仓", ""])

            service.sell(
                "600000",
                date(2024, 6, 3),
                50,
                "12.00",
                notes={"sell_reason": "达到目标价", "strategy_changed": "否"},
            )
            # 重新建服务实例，确认字段真正落库而不是只存在内存
            reloaded = LedgerService(conn)
            sells = [t for t in reloaded.portfolio.trades if t.side == "SELL"]
            self.assertEqual(sells[0].sell_reason, "达到目标价")
            self.assertEqual(sells[0].strategy_changed, "否")
            buys = [t for t in reloaded.portfolio.trades if t.side == "BUY"]
            self.assertEqual(buys[0].target_price_mills, 13_500)
            self.assertEqual(buys[0].hold_period, "长期")

            # 修改成交时可整组改写决策记录（右键 → 修改）
            reloaded.update_trade(
                sells[0].trade_id,
                symbol="600000",
                trade_date=date(2024, 6, 3),
                qty=50,
                price_yuan="12.00",
                notes={
                    "trade_logic": "达到目标价止盈",
                    "plan_type": "计划外",
                    "strategy_changed": "是",
                },
            )
            sells = [t for t in reloaded.portfolio.trades if t.side == "SELL"]
            self.assertEqual(sells[0].trade_logic, "达到目标价止盈")
            self.assertEqual(sells[0].plan_type, "计划外")
            self.assertEqual(sells[0].strategy_changed, "是")
            self.assertEqual(sells[0].sell_reason, "")  # 整组覆盖，旧值清空
            buys = [t for t in reloaded.portfolio.trades if t.side == "BUY"]
            self.assertEqual(buys[0].trade_logic, "低估分批建仓")
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
