"""交割单智能解析与批量导入测试。

自造两份格式完全不同的假交割单（同花顺式 UTF-8 逗号 CSV、华泰式 GBK 制表符 CSV），
都带表头废话行与银行转账 / 红利入账 / 申购记录，跑通：
1. 列名嗅探、冗余列剔除、表头定位、编码兼容；
2. 非交易记录隔离，不写入账本；
3. 日期 / 金额（分）/ 价格（毫）标准化；
4. 批量入库后 FIFO 批次正确；
5. UI 后台线程不阻塞主线程，预览与确认导入全流程可用。

运行：python tests/test_statement_import.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.statement_import import (  # noqa: E402
    StatementParseError,
    normalize_symbol,
    parse_cents,
    parse_date,
    parse_price_mills,
    parse_statement,
)

# 同花顺式：UTF-8、逗号分隔、3 行废话、13 列表头、含银行转账 / 红利入账 / 申购
STATEMENT_A = """同花顺·历史交割单（示例文件）
账户：测试账户    导出时间：2026-11-07 09:30
本文件仅供对账参考
成交日期,证券代码,证券名称,买卖标志,成交均价,成交数量,成交金额,手续费,印花税,过户费,流水号,资金余额,委托号
2024-01-02,600000,浦发银行,买入,10.00,1000,10000.00,5.00,0.00,0.20,20240102001,90000.00,123456
2024-01-05,600000,浦发银行,卖出,12.00,500,6000.00,5.00,3.00,0.10,20240105001,96000.00,123457
2024-01-06,,,银行转证券,,,20000.00,,,20240106001,116000.00,
2024-01-08,600000,浦发银行,红利入账,,,420.00,,,20240108001,116420.00,
2024-01-10,600519,贵州茅台,申购,,,,,,,,20240110001,116420.00,
2024-01-12,000001,平安银行,买入,9.500,2000,19000.00,5.00,,0.38,20240112001,97415.00,
"""

# 华泰式：GBK、制表符分隔、不同列名、另含「备注」干扰列
STATEMENT_B = """华泰证券 客户交割明细
打印时间：2026-11-07 09:00:00
序号\t发生日期\t证券编码\t证券名称\t操作\t成交价格\t成交股数\t佣金\t备注
1\t20240115\t510300\t沪深300ETF\t证券买入\t4.012\t1500\t5\t
2\t20240118\t510300\t沪深300ETF\t证券卖出\t4.120\t800\t5\t
3\t20240120\t\t\t银行转证券\t\t\t\t
4\t20240122\t510300\t沪深300ETF\t申购\t\t\t\t
5\t20240125\t510300\t沪深300ETF\t证券买入\t4.015\t200\t5\t红利入账
"""


# 交割单口径：费用分项与发生金额必须原样入账，不得按费率引擎重算
STATEMENT_MODE = """证券代码,证券名称,操作,成交数量,成交均价,发生金额,佣金,印花税,过户费,交收日期
,,银行转证券,0,0.000,2000.000,0.00,0.00,0.00,20240228
600000,浦发银行,证券买入,100,10.000,-1005.000,2.00,0.00,0.03,20240301
"""

# 含「发生金额」的真实现金流水：银行入金 / 红利 / 利息 / 逆回购
STATEMENT_C = """证券代码,证券名称,操作,成交数量,成交均价,发生金额,交收日期
,,银行转证券,0,0.000,20000.000,20240201
002027,分众传媒,红利入账,0,0.000,200.000,20240205
002027,分众传媒,证券买入,100,10.000,-1000.000,20240206
,,利息归本,0,0.000,1.200,20240208
204001,GC001,通用回购逆回,10,0.000,-1000.000,20240209
"""


# 另一家券商的交割单：含银行入金、红利税、红股入账、逆回购、卖出，并带「资金余额」列。
# 用来验证「换一家券商全量导入后，账面现金必须与末行资金余额逐分一致」。
OTHER_BROKER = """证券代码,证券名称,操作,成交数量,成交均价,发生金额,佣金,印花税,过户费,资金余额,成交日期,交收日期
,,银行转证券,0,0.000,20000.00,0.00,0.00,0.00,20000.00,20240201,20240201
600000,浦发银行,证券买入,100,10.000,-1005.00,2.00,0.00,0.03,18995.00,20240202,20240205
600000,浦发银行,红利入账,0,0.000,200.00,0.00,0.00,0.00,19195.00,,20240208
600000,浦发银行,股息红利差异,0,0.000,-20.00,0.00,0.00,0.00,19175.00,,20240208
600000,浦发银行,红股入账,10,0.000,0.00,0.00,0.00,0.00,19175.00,20240209,20240209
,,通用回购逆回 R-001,0,0.000,-1000.02,0.00,0.00,0.00,18174.98,20240210,20240210
,,通用回购逆回 R-001 到期,0,0.000,1000.08,0.00,0.00,0.00,19175.06,20240211,20240211
600000,浦发银行,证券卖出,110,11.000,1204.87,5.12,0.00,0.01,20379.93,20240212,20240212
"""


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    from desktop.qt import QApplication

    deadline = time.time() + timeout
    while time.time() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class StatementFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path_a = Path(self.tmp) / "同花顺交割单.csv"
        self.path_b = Path(self.tmp) / "华泰交割单.csv"
        self.path_c = Path(self.tmp) / "含现金流水交割单.csv"
        self.path_a.write_text(STATEMENT_A, encoding="utf-8")
        self.path_b.write_text(STATEMENT_B, encoding="gbk")
        self.path_c.write_text(STATEMENT_C, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _import_payload(self, result) -> list[dict]:
        return [
            {
                "side": trade.side,
                "symbol": trade.symbol,
                "trade_date": trade.trade_date,
                "qty": trade.qty,
                "price_mills": trade.price_mills,
            }
            for trade in result.trades
        ]

    @staticmethod
    def _cash_payload(result) -> list[dict]:
        return [
            {
                "entry_date": flow.entry_date,
                "amount_cents": flow.amount_cents,
                "kind": flow.kind,
                "memo": flow.memo,
            }
            for flow in result.cash_flows
        ]


class TestStatementParser(StatementFixture):
    def test_utf8_csv_sniffs_columns_and_isolates_non_trades(self) -> None:
        result = parse_statement(self.path_a)

        self.assertEqual(
            result.mapping,
            {
                "ticker": "证券代码",
                "name": "证券名称",
                "date": "成交日期",
                "action": "买卖标志",
                "qty": "成交数量",
                "price": "成交均价",
                "commission": "手续费",
                "stamp": "印花税",
                "transfer": "过户费",
                "balance": "资金余额",
            },
        )
        # 流水号、委托号、成交金额（非发生金额）全部剔除
        for redundant in ("流水号", "委托号", "成交金额"):
            self.assertIn(redundant, result.ignored_headers)

        self.assertEqual(len(result.trades), 3)
        self.assertEqual(result.non_trade_count, 3)
        self.assertEqual(result.invalid_count, 0)

        first, second, third = result.trades
        self.assertEqual(first.trade_date, date(2024, 1, 2))
        self.assertEqual(first.symbol, "600000")
        self.assertEqual(first.side, "BUY")
        self.assertEqual(first.qty, 1000)
        self.assertEqual(first.price_mills, 10_000)      # 10.00 元 -> 10000 毫
        self.assertEqual(first.commission_cents, 500)    # 5.00 元 -> 500 分
        self.assertEqual(first.name, "浦发银行")

        self.assertEqual(second.side, "SELL")
        self.assertEqual(second.qty, 500)
        self.assertEqual(second.price_mills, 12_000)

        self.assertEqual(third.symbol, "000001")
        self.assertEqual(third.price_mills, 9_500)       # 9.500 元 -> 9500 毫

    def test_gbk_tab_csv_with_different_headers(self) -> None:
        result = parse_statement(self.path_b)

        self.assertEqual(result.mapping["ticker"], "证券编码")
        self.assertEqual(result.mapping["date"], "发生日期")
        self.assertEqual(result.mapping["action"], "操作")
        self.assertEqual(result.mapping["qty"], "成交股数")
        self.assertEqual(result.mapping["price"], "成交价格")
        self.assertEqual(result.mapping["commission"], "佣金")

        self.assertEqual(len(result.trades), 3)
        self.assertEqual(result.non_trade_count, 2)
        self.assertIn("备注", result.ignored_headers)

        first = result.trades[0]
        self.assertEqual(first.trade_date, date(2024, 1, 15))  # 20240115 紧凑日期
        self.assertEqual(first.symbol, "510300")
        self.assertEqual(first.price_mills, 4_012)             # ETF 三位小数
        self.assertEqual(first.qty, 1500)
        self.assertEqual(first.commission_cents, 500)
        # 备注列的「红利入账」不影响正常买卖行，说明冗余列确实被丢弃
        self.assertEqual(result.trades[-1].side, "BUY")

    def test_text_disguised_as_xls(self) -> None:
        """券商常把 GBK 制表符文本存成 .xls：不能按扩展名硬走 xlrd。"""
        path = Path(self.tmp) / "伪装成xls的交割单.xls"
        content = (
            "证券代码\t证券名称\t操作\t成交数量\t成交均价\t手续费\t交收日期\n"
            "600519\t贵州茅台\t买入\t100\t1600.500\t5.00\t20241011\n"
            "204001\t通用回购逆回\t通用回购逆回\t100\t0.000\t0.00\t20241011\n"
            "000001\t平安银行\t卖出\t200\t12.300\t5.00\t20241012\n"
        )
        path.write_bytes(content.encode("gbk"))

        result = parse_statement(path)

        # 文件里只有交收日期时，退回用它作为成交日期
        self.assertEqual(result.mapping["date"], "交收日期")
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.non_trade_count, 1)
        self.assertEqual(result.invalid_count, 0)
        self.assertEqual(result.trades[0].trade_date, date(2024, 10, 11))
        self.assertEqual(result.trades[1].side, "SELL")

    def test_cash_flows_are_captured_from_amount_column(self) -> None:
        """红利/利息按「发生金额」补入现金；银行入金与逆回购只统计不入账。"""
        result = parse_statement(self.path_c)

        self.assertEqual(result.mapping["amount"], "发生金额")
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.non_trade_count, 4)

        self.assertEqual(result.cash_flow_count, 2)
        self.assertEqual(result.cash_flow_cents, 20_120)      # 200 元红利 + 1.2 元利息
        # 逆回购本金不算现金收支：cash_flows 不含 REPO，只保留银行与红利/利息
        self.assertEqual(
            [flow.kind for flow in result.cash_flows],
            ["DEPOSIT", "STATEMENT_DIVIDEND", "STATEMENT_INTEREST"],
        )
        self.assertEqual(
            [flow.kind for flow in result.income_flows],
            ["STATEMENT_DIVIDEND", "STATEMENT_INTEREST"],
        )
        self.assertEqual(result.income_flows[0].entry_date, date(2024, 2, 5))
        self.assertEqual(result.bank_transfer_cents, 2_000_000)
        self.assertEqual(result.repo_cents, -100_000)
        self.assertEqual(result.repo_outstanding_cents, 100_000)
        self.assertEqual(result.repo_interest_cents, 0)

    def test_repo_principal_is_parked_and_interest_is_income(self) -> None:
        """逆回购本金留在可用资金里，到期差额才是利息收入。"""
        path = Path(self.tmp) / "逆回购交割单.csv"
        path.write_text(
            "证券代码,证券名称,操作,成交数量,成交均价,发生金额,交收日期\n"
            ",,通用回购逆回 R-001,0,0.000,-2000.020,20240110\n"
            ",,通用回购逆回 R-001,0,0.000,2000.080,20240111\n"
            ",,通用回购逆回 GC001,0,0.000,-12000.120,20240115\n",
            encoding="utf-8",
        )
        result = parse_statement(path)

        self.assertFalse([flow for flow in result.cash_flows if flow.kind == "REPO"])
        interest = [flow for flow in result.cash_flows if flow.kind == "STATEMENT_INTEREST"]
        self.assertEqual([flow.amount_cents for flow in interest], [6])  # 0.06 元
        self.assertEqual(result.repo_interest_cents, 6)
        self.assertEqual(result.repo_outstanding_cents, 1_200_012)
        self.assertEqual(result.repo_cents, -1_200_006)

    def test_repo_repayment_without_open_leg_keeps_principal_out(self) -> None:
        """期初持有的回购到期：本金已在可用资金里，只把超出本金的差额记利息。"""
        path = Path(self.tmp) / "期初回购到期.txt"
        path.write_text(
            "证券代码,证券名称,操作,成交数量,成交均价,成交金额,发生金额,资金余额,交收日期\n"
            "204001,GC001,通用回购逆回,20,1.235,2000.000,2000.070,2669.780,20261109\n"
            "204001,GC001,通用回购逆回,20,1.040,2000.000,-2000.020,669.760,20261109\n",
            encoding="utf-8",
        )
        result = parse_statement(path)

        # 到期的本金不能重复入账，否则现金会比券商余额多出 2000 元
        self.assertEqual([flow.kind for flow in result.cash_flows], ["STATEMENT_INTEREST"])
        self.assertEqual(result.cash_flows[0].amount_cents, 7)
        self.assertEqual(result.repo_interest_cents, 7)
        self.assertEqual(result.repo_outstanding_cents, 200_002)

    def test_excel_with_preamble_rows(self) -> None:
        try:
            from openpyxl import Workbook
        except ImportError:  # pragma: no cover
            self.skipTest("未安装 openpyxl")

        path = Path(self.tmp) / "excel交割单.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["某券商历史成交明细"])
        sheet.append([])
        sheet.append(["成交日期", "证券代码", "买卖方向", "成交价格", "成交数量", "佣金"])
        sheet.append([datetime(2024, 2, 1), "600519", "买入", 1600.5, 100, 5])
        workbook.save(path)

        result = parse_statement(path)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.trade_date, date(2024, 2, 1))
        self.assertEqual(trade.price_mills, 1_600_500)
        self.assertEqual(trade.commission_cents, 500)

    def test_missing_key_column_raises_clear_message(self) -> None:
        path = Path(self.tmp) / "缺代码列.csv"
        path.write_text(
            "成交日期,买卖标志,成交均价,成交数量\n2024-01-02,买入,10.00,1000\n",
            encoding="utf-8",
        )
        with self.assertRaises(StatementParseError) as ctx:
            parse_statement(path)
        self.assertIn("未能识别出证券代码列", str(ctx.exception))

    def test_no_header_raises_clear_message(self) -> None:
        path = Path(self.tmp) / "没有表头.csv"
        path.write_text("这是一份随便的文件\n没有任何表头\n", encoding="utf-8")
        with self.assertRaises(StatementParseError) as ctx:
            parse_statement(path)
        self.assertIn("未能找到交割单表头行", str(ctx.exception))

    def test_primitive_normalization(self) -> None:
        self.assertEqual(normalize_symbol("600000.SH"), "600000")
        self.assertEqual(normalize_symbol("SH600000"), "600000")
        self.assertEqual(normalize_symbol(600000.0), "600000")
        # Excel 把代码存成数字时前导零会丢：000001 -> 1，必须补齐 6 位
        self.assertEqual(normalize_symbol(1), "000001")
        self.assertEqual(normalize_symbol(2027), "002027")
        self.assertEqual(parse_date("2024年01月02日"), date(2024, 1, 2))
        self.assertEqual(parse_date(45293), date(2024, 1, 2))       # Excel 序列号
        self.assertEqual(parse_cents("￥1,234.56"), 123_456)
        self.assertEqual(parse_cents("5.00-"), -500)
        self.assertEqual(parse_price_mills("4.005元"), 4_005)


class TestHeaderRobustness(StatementFixture):
    """券商表头排版差异：同义词、杂乱列序、委托列干扰、错列保护。"""

    def _write(self, name: str, text: str) -> Path:
        path = Path(self.tmp) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_synonym_headers_and_reordered_columns(self) -> None:
        path = self._write(
            "券商丙.csv",
            "序号,业务名称,股票代号,股票名称,发生日期,成交股数,成交单价,发生金额,规费,印花税,过户费,资金余额\n"
            "1,证券买入,000001,平安银行,20240301,1000,9.500,-9505.00,5.00,0.00,0.19,90495.00\n"
            "2,证券卖出,000001,平安银行,20240305,500,10.200,5092.45,5.00,2.55,0.10,95585.00\n",
        )
        result = parse_statement(path)
        self.assertEqual(result.mapping["ticker"], "股票代号")
        self.assertEqual(result.mapping["action"], "业务名称")
        self.assertEqual(result.mapping["qty"], "成交股数")
        self.assertEqual(result.mapping["price"], "成交单价")
        self.assertEqual(result.mapping["amount"], "发生金额")
        self.assertEqual(result.mapping["other_fee"], "规费")
        trade = result.trades[0]
        self.assertEqual((trade.side, trade.symbol, trade.qty), ("BUY", "000001", 1000))
        self.assertEqual(trade.price_mills, 9500)
        self.assertEqual(trade.amount_cents, -950500)

    def test_share_balance_is_not_cash_balance(self) -> None:
        # 同时有「资金余额」与「股票余额」：一个管钱、一个管股，不能互相抢占
        path = self._write(
            "券商戊.csv",
            "证券代码,证券名称,操作,成交数,成交均价,发生金额,资金余额,股票余额,成交日期\n"
            "601919,中远海控,证券买入,100,14.500,-1451.01,98548.99,500,20260318\n"
            "601919,中远海控,证券卖出,100,16.080,1606.00,100154.99,400,20260818\n",
        )
        result = parse_statement(path)
        self.assertEqual(result.mapping["balance"], "资金余额")
        self.assertEqual(result.mapping["share_balance"], "股票余额")
        self.assertEqual(result.share_balances, {"601919": 400})
        self.assertEqual(result.final_balance_cents, 10_015_499)

    def test_delegate_columns_lose_to_trade_columns(self) -> None:
        path = self._write(
            "券商丁.csv",
            "证券代码,证券名称,买卖标志,委托数量,委托价格,成交数量,成交价格,发生金额,成交日期\n"
            "600000,浦发银行,买入,1000,9.900,1000,9.950,-9955.00,20240301\n",
        )
        result = parse_statement(path)
        self.assertEqual(result.mapping["qty"], "成交数量")
        self.assertEqual(result.mapping["price"], "成交价格")

    def test_shifted_columns_are_rejected(self) -> None:
        # 表头标签与实际数据错位：价格列塞了数量、数量列塞了价格
        path = self._write(
            "错列.csv",
            "证券代码,证券名称,买卖标志,成交价格,成交数量,发生金额,成交日期\n"
            "600000,浦发银行,买入,1000,10,-1000.00,20240301\n",
        )
        with self.assertRaises(StatementParseError) as ctx:
            parse_statement(path)
        self.assertIn("列识别错位", str(ctx.exception))

    def test_name_column_cannot_be_symbol(self) -> None:
        path = self._write(
            "缺代码.csv",
            "证券名称,买卖标志,成交数量,成交价格,发生金额,成交日期\n"
            "浦发银行,买入,1000,9.950,-9955.00,20240301\n",
        )
        with self.assertRaises(StatementParseError) as ctx:
            parse_statement(path)
        self.assertIn("证券代码列", str(ctx.exception))


class TestDetailRowWidth(unittest.TestCase):
    """标的明细的行宽必须与表头严格一致，防止整行右移再次发生。"""

    def test_trade_rows_match_detail_headers(self) -> None:
        from core.trading import Portfolio
        from desktop.dialogs import TRADE_DETAIL_HEADERS
        from desktop.models import trade_record_row

        portfolio = Portfolio(initial_cash_cents=1_000_000)
        buy = portfolio.buy("600000", date(2024, 1, 2), 100, 10_000)
        lot = portfolio.books["600000"].lots[0]
        sell = portfolio.sell("600000", date(2024, 1, 5), 100, 12_000)

        buy_row = trade_record_row(buy, with_symbol=False, lot=lot)
        sell_row = trade_record_row(sell, with_symbol=False, batch_label="1")
        self.assertEqual(len(buy_row.values), len(TRADE_DETAIL_HEADERS))
        self.assertEqual(len(sell_row.values), len(TRADE_DETAIL_HEADERS))
        self.assertEqual(sell_row.values[-2], f"{sell.net_cash_cents / 100:+,.2f}")
        self.assertEqual(sell_row.values[-1], f"{sell.realized_pnl_cents / 100:+,.2f}")

        # 红利税与红利状态在「累计分红」之后，公司行为的列项不再参与
        taxed = trade_record_row(
            buy,
            with_symbol=False,
            lot=lot,
            dividend_cents=20_000,
            dividend_tax_cents=2_000,
            dividend_status="已扣缴",
        )
        self.assertEqual(len(taxed.values), len(TRADE_DETAIL_HEADERS))
        self.assertEqual(taxed.values[7], "200.00")
        self.assertEqual(taxed.values[8], "20.00")
        self.assertEqual(taxed.values[9], "已扣缴")


class TestStatementLedgerImport(StatementFixture):
    def setUp(self) -> None:
        super().setUp()
        from data.db import open_database
        from services.ledger import LedgerService

        self.conn = open_database(Path(self.tmp) / "longledger.db")
        self.ledger = LedgerService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_batch_import_writes_trades_and_builds_fifo_lots(self) -> None:
        result = parse_statement(self.path_a)
        count = self.ledger.import_trades(self._import_payload(result))
        self.assertEqual(count, 3)

        trades = self.ledger.portfolio.trades
        self.assertEqual(len(trades), 3)
        self.assertEqual([t.symbol for t in trades], ["600000", "600000", "000001"])
        self.assertEqual([t.side for t in trades], ["BUY", "SELL", "BUY"])
        # 非交易记录没有被写成任何一笔成交
        self.assertNotIn("", {t.symbol for t in trades})
        self.assertTrue(all(t.qty > 0 for t in trades))

        book = self.ledger.portfolio.book("600000")
        self.assertEqual(book.total_qty, 500)
        self.assertEqual(len(book.lots), 1)
        self.assertEqual(book.lots[0].qty_opened, 1000)
        self.assertEqual(book.lots[0].qty_remaining, 500)

        # FIFO 审计链：卖出的 500 股全部来自第一批（load_portfolio 不装载审计链，直查库表）
        consumption_rows = self.ledger.repo.conn.execute(
            "SELECT sell_trade_id, lot_id, qty FROM lot_consumptions WHERE account_id = ?",
            (self.ledger.account_id,),
        ).fetchall()
        self.assertEqual(len(consumption_rows), 1)
        self.assertEqual(consumption_rows[0]["sell_trade_id"], trades[1].trade_id)
        self.assertEqual(consumption_rows[0]["lot_id"], book.lots[0].lot_id)
        self.assertEqual(consumption_rows[0]["qty"], 500)

        # 佣金按费率引擎重算（最低 5 元保底），而非直接采用文件里的手续费
        self.assertEqual(trades[0].commission_cents, 500)
        self.assertEqual(trades[1].commission_cents, 500)
        self.assertEqual(trades[1].stamp_tax_cents, 300)

        # 重新打开连接仍能看到全部成交（确认已落库）
        from services.ledger import LedgerService

        reloaded = LedgerService(self.conn)
        self.assertEqual(len(reloaded.portfolio.trades), 3)
        self.assertEqual(reloaded.portfolio.book("600000").total_qty, 500)

    def test_second_format_import_is_clean(self) -> None:
        result = parse_statement(self.path_b)
        count = self.ledger.import_trades(self._import_payload(result))
        self.assertEqual(count, 3)

        book = self.ledger.portfolio.book("510300")
        self.assertEqual(book.total_qty, 900)          # 1500 - 800 + 200
        self.assertEqual(len(book.lots), 2)
        self.assertEqual([lot.qty_remaining for lot in book.lots], [700, 200])
        self.assertNotIn("", {t.symbol for t in self.ledger.portfolio.trades})

    def test_cash_flows_rescue_import_from_insufficient_cash(self) -> None:
        """本金只够成交额时，补入红利/利息才能让整段重放不中途见底。"""
        result = parse_statement(self.path_c)
        self.ledger.set_initial_capital(100_000)          # 1000 元，买入后仅剩 −5 元

        with self.assertRaises(ValueError):
            self.ledger.import_trades(self._import_payload(result))

        count = self.ledger.import_trades(
            self._import_payload(result), self._cash_payload(result)
        )
        self.assertEqual(count, 1)
        # 1000 元本金 + 200 元红利 + 1.2 元利息 − 1000 元成交额 − 5 元佣金 − 0.01 元过户费
        self.assertEqual(self.ledger.portfolio.cash_cents, 19_619)
        self.assertEqual(
            [e.kind for e in self.ledger.portfolio.ledger if e.kind.startswith("STATEMENT")],
            ["STATEMENT_DIVIDEND", "STATEMENT_INTEREST"],
        )

        # 改单 / 删单会全量重放，补入的现金流水必须仍在库里回放
        self.ledger.rebuild_all()
        self.assertEqual(self.ledger.portfolio.cash_cents, 19_619)

    def test_failed_import_keeps_database_untouched(self) -> None:
        result = parse_statement(self.path_a)
        self.ledger.import_trades(self._import_payload(result))
        before = len(self.ledger.repo.list_trades(limit=1000))

        with self.assertRaises(ValueError):
            self.ledger.import_trades(
                [
                    {
                        "side": "SELL",
                        "symbol": "600000",
                        "trade_date": date(2024, 3, 1),
                        "qty": 9999,       # 持仓不足，重放必须失败
                        "price_mills": 12_000,
                    }
                ]
            )

        self.assertEqual(len(self.ledger.repo.list_trades(limit=1000)), before)


class TestStatementModeLedger(StatementFixture):
    """交割单口径：不重算费率与红利，现金完全按发生金额累计。"""

    def setUp(self) -> None:
        super().setUp()
        from data.db import open_database
        from data.repositories import Repository
        from services.ledger import LedgerService

        self.conn = open_database(Path(self.tmp) / "statement_mode.db")
        repo = Repository(self.conn)
        self.account_id = repo.create_account("交割单口径账户", 0, "STATEMENT")
        self.conn.commit()
        self.ledger = LedgerService(self.conn, self.account_id)
        self.path_mode = Path(self.tmp) / "交割单口径.csv"
        self.path_mode.write_text(STATEMENT_MODE, encoding="utf-8")

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def _payload(self, result) -> list[dict]:
        return [
            {
                "side": trade.side,
                "symbol": trade.symbol,
                "trade_date": trade.trade_date,
                "qty": trade.qty,
                "price_mills": trade.price_mills,
                "amount_cents": trade.amount_cents,
                "commission_cents": trade.commission_cents,
                "stamp_tax_cents": trade.stamp_tax_cents,
                "transfer_fee_cents": trade.transfer_fee_cents,
                "other_fee_cents": trade.other_fee_cents,
            }
            for trade in result.trades
        ]

    def test_statement_dividends_feed_dual_cost_display(self) -> None:
        """交割单口径的红利/红利税按持股数摊回批次；汇总摊薄成本按券商税前分红口径。"""
        path = Path(self.tmp) / "交割单红利.csv"
        path.write_text(
            "证券代码,证券名称,操作,成交数量,成交均价,发生金额,佣金,印花税,过户费,交收日期\n"
            ",,银行转证券,0,0.000,2000.000,0.00,0.00,0.00,20240301\n"
            "600000,浦发银行,证券买入,100,10.000,-1005.030,2.00,0.00,0.03,20240302\n"
            "600000,浦发银行,红利入账,0,0.000,20.000,0.00,0.00,0.00,20240305\n"
            "600000,浦发银行,股息红利差异,0,0.000,-2.000,0.00,0.00,0.00,20240305\n",
            encoding="utf-8",
        )
        result = parse_statement(path)
        self.ledger.import_trades(self._payload(result), self._cash_payload(result))

        # 期初现金为 0 时，净值基准取交割单里的银行转入流水
        self.assertEqual(self.ledger.capital_base_cents(), 200_000)

        lot = self.ledger.portfolio.book("600000").lots[0]
        self.assertEqual(lot.cost_cents, 100_503)
        self.assertEqual(lot.received_div_cents, 1_800)       # 红利 20 − 红利税 2
        self.assertEqual(lot.original_cost_mills, 10_050)     # 10.0503 元/股，四舍五入到毫
        self.assertEqual(lot.diluted_cost_mills, 9_870)       # (1005.03 − 18) / 100

        self.ledger.set_close("600000", date(2024, 3, 6), "11.00")
        row = self.ledger.holdings_summary(date(2024, 3, 6))[0]
        self.assertEqual(row["原始成本(元/股)"], 10.05)
        # 券商口径用税前分红：10.0503 − 0.20 = 9.8503，四舍五入 9.850
        self.assertEqual(row["摊薄成本(元/股)"], 9.85)
        self.assertEqual(row["累计现金分红(元)"], 20.0)
        # 市值 1100 − 摊薄总成本 985.03 = 114.97（红利税已体现在已实现盈亏）
        self.assertEqual(row["持仓盈亏(元)"], 114.97)

    def test_fees_and_cash_use_statement_values(self) -> None:
        result = parse_statement(self.path_mode)
        self.assertEqual(len(result.trades), 1)
        count = self.ledger.import_trades(
            self._payload(result), self._cash_payload(result)
        )
        self.assertEqual(count, 1)

        trade = self.ledger.portfolio.trades[0]
        # 费率引擎会给最低 5 元佣金；交割单口径必须采用原值 2.00 元与过户费 0.03 元
        self.assertEqual(trade.commission_cents, 200)
        self.assertEqual(trade.stamp_tax_cents, 0)
        self.assertEqual(trade.transfer_fee_cents, 3)
        self.assertEqual(trade.net_cash_cents, -100_500)
        # 期初 0 + 银行入金 2000 元 − 发生金额 1005 元
        self.assertEqual(self.ledger.portfolio.cash_cents, 99_500)
        kinds = [entry.kind for entry in self.ledger.portfolio.ledger]
        self.assertIn("DEPOSIT", kinds)
        self.assertIn("TRADE_BUY", kinds)

    def test_reimport_skips_trades_and_cash_flows(self) -> None:
        result = parse_statement(self.path_mode)
        payload = self._payload(result)
        cash = self._cash_payload(result)
        self.ledger.import_trades(payload, cash)
        before = self.ledger.portfolio.cash_cents

        again = self.ledger.import_trades(payload, cash)
        self.assertEqual(again, 0)
        self.assertEqual(self.ledger.last_import_duplicates, 2)  # 1 笔成交 + 1 笔银行流水
        self.assertEqual(self.ledger.portfolio.cash_cents, before)

    def test_manual_operations_are_disabled(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.ledger.buy("600000", date(2024, 3, 1), 100, "10.00")
        self.assertIn("交割单口径", str(ctx.exception))
        with self.assertRaises(ValueError):
            self.ledger.deposit(10_000)
        with self.assertRaises(ValueError):
            self.ledger.delete_trades([1])

    def test_rebuild_keeps_statement_cash_and_fees(self) -> None:
        result = parse_statement(self.path_mode)
        self.ledger.import_trades(self._payload(result), self._cash_payload(result))
        before = self.ledger.portfolio.cash_cents
        commission = self.ledger.portfolio.trades[0].commission_cents

        self.ledger.rebuild_all()

        self.assertEqual(self.ledger.portfolio.cash_cents, before)
        self.assertEqual(self.ledger.portfolio.trades[0].commission_cents, commission)

    def test_initial_capital_is_opening_cash(self) -> None:
        result = parse_statement(self.path_mode)
        self.ledger.import_trades(self._payload(result), self._cash_payload(result))
        # 交割单口径下改期初现金必须同步现金余额（系统口径此时只调净值基准）
        self.ledger.set_initial_capital(50_000)
        self.assertEqual(self.ledger.portfolio.cash_cents, 99_500 + 50_000)


class TestCrossBrokerReconciliation(StatementFixture):
    """换一家券商的交割单：账面现金必须与文件末行「资金余额」逐分一致。

    覆盖跨券商最容易踩的四个坑：红利行只填交收日期、红利税是负数、
    红股入账不带现金、逆回购开仓/到期备注写法不同。
    """

    def setUp(self) -> None:
        super().setUp()
        from data.db import open_database
        from data.repositories import Repository
        from services.ledger import LedgerService

        self.conn = open_database(Path(self.tmp) / "cross_broker.db")
        repo = Repository(self.conn)
        self.account_id = repo.create_account("外部券商账户", 0, "STATEMENT")
        self.conn.commit()
        self.ledger = LedgerService(self.conn, self.account_id)
        self.path_other = Path(self.tmp) / "华泰式交割单.csv"
        self.path_other.write_text(OTHER_BROKER, encoding="utf-8")

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def _full_payload(self, result) -> tuple[list[dict], list[dict]]:
        trades = [
            {
                "side": trade.side,
                "symbol": trade.symbol,
                "trade_date": trade.trade_date,
                "qty": trade.qty,
                "price_mills": trade.price_mills,
                "amount_cents": trade.amount_cents,
                "commission_cents": trade.commission_cents,
                "stamp_tax_cents": trade.stamp_tax_cents,
                "transfer_fee_cents": trade.transfer_fee_cents,
                "other_fee_cents": trade.other_fee_cents,
                "seq": trade.source_row,
            }
            for trade in result.trades
        ]
        trades.extend(
            {
                "side": "BUY",
                "symbol": share.symbol,
                "trade_date": share.entry_date,
                "qty": share.qty,
                "price_mills": 0,
                "share_entry": True,
                "amount_cents": 0,
                "seq": share.source_row,
                "memo": share.memo,
            }
            for share in result.shares
        )
        cash = [
            {
                "entry_date": flow.entry_date,
                "amount_cents": flow.amount_cents,
                "kind": flow.kind,
                "memo": flow.memo,
                "seq": flow.source_row,
            }
            for flow in result.cash_flows
        ]
        return trades, cash

    def test_full_import_reconciles_with_statement_balance(self) -> None:
        result = parse_statement(self.path_other)

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(len(result.shares), 1)
        self.assertEqual(result.shares[0].symbol, "600000")
        self.assertEqual(result.shares[0].qty, 10)
        # 红利行「成交日期」为空，必须退回交收日期 2024-02-08
        dividend = [
            flow for flow in result.cash_flows if flow.kind == "STATEMENT_DIVIDEND"
        ][0]
        self.assertEqual(dividend.entry_date, date(2024, 2, 8))
        self.assertEqual(dividend.amount_cents, 20_000)
        self.assertEqual(
            [flow.amount_cents for flow in result.income_flows
             if flow.kind == "STATEMENT_DIVIDEND_TAX"],
            [-2_000],
        )
        # 逆回购到期备注与开仓不同，仍应认出同一笔并只把 0.06 元差额记为利息
        self.assertEqual(result.repo_interest_cents, 6)
        self.assertEqual(result.repo_outstanding_cents, 0)
        self.assertEqual(result.final_balance_cents, 2_037_993)

        trades, cash = self._full_payload(result)
        count = self.ledger.import_trades(trades, cash)
        self.assertEqual(count, 3)                       # 2 笔成交 + 1 笔红股
        self.assertEqual(self.ledger.portfolio.cash_cents, 2_037_993)
        self.assertEqual(self.ledger.portfolio.book("600000").total_qty, 0)

        # 卖出 110 股 = 买入 100 股 + 红股 10 股。
        # 已实现盈亏按券商口径：卖出净额 −（买入成本 − 持有期现金分红）
        # 买入成本 100,500（含 5.00 费用），持有期红利 200.00 摊薄成本，
        # 另有一笔 20.00 股息红利税属于分红的追回，故摊薄成本为 100,500 − 20,000 + 2,000
        sell = [t for t in self.ledger.portfolio.trades if t.side == "SELL"][0]
        self.assertEqual(sell.realized_pnl_cents, 120_487 - (100_500 - 20_000 + 2_000))

        # 重复导入：成交、红股、现金流水全部按条数核销，余额分毫不动
        again = self.ledger.import_trades(trades, cash)
        self.assertEqual(again, 0)
        self.assertEqual(self.ledger.last_import_duplicates, 7)
        self.assertEqual(self.ledger.portfolio.cash_cents, 2_037_993)

        # 全量重放后红股批次仍在（0 成本批次必须持久化，不能只存在内存里）
        self.ledger.rebuild_all()
        self.assertEqual(self.ledger.portfolio.cash_cents, 2_037_993)
        self.assertEqual(self.ledger.portfolio.book("600000").total_qty, 0)

        # 交割单口径的公司行为由红利流水与 0 成本红股派生（不写 corporate_actions）
        self.assertEqual(self.ledger.repo.list_corporate_actions(), [])
        derived = self.ledger.statement_corporate_actions()
        self.assertEqual(
            {row["类型"] for row in derived},
            {"现金分红", "股息红利税", "红股/送转入账"},
        )
        dividend_row = [row for row in derived if row["类型"] == "现金分红"][0]
        self.assertEqual(dividend_row["代码"], "600000")
        self.assertEqual(dividend_row["派现(元)"], 200.0)
        share_row = [row for row in derived if row["类型"] == "红股/送转入账"][0]
        self.assertEqual(share_row["新增股数"], 10)
        # 单标的过滤只留该标的的行
        self.assertEqual(
            len(self.ledger.statement_corporate_actions("600000")), len(derived)
        )
        self.assertEqual(self.ledger.statement_corporate_actions("510300"), [])

        # 标的明细口径：按批次拆成与系统口径一致的列项
        batch_rows = self.ledger.statement_corporate_actions("600000", batch_level=True)
        self.assertEqual({row["批次"] for row in batch_rows}, {1})
        dividend_lot = [r for r in batch_rows if r["类型"] == "现金分红"][0]
        self.assertEqual(dividend_lot["每股派息(元)"], 2.0)
        self.assertEqual(dividend_lot["本批次红利(元)"], 200.0)
        self.assertEqual(dividend_lot["红利状态"], "WITHHELD")
        share_lot = [r for r in batch_rows if r["类型"] in ("送股", "转增")][0]
        self.assertEqual(share_lot["每股送股"] + share_lot["每股转增"], 0.1)
        self.assertEqual(share_lot["红利状态"], "未参与")

        # 红利税与状态移到买卖明细：按批次汇总，税额取交割单税单原值
        facts = self.ledger.dividend_facts("600000")
        self.assertEqual(facts[1]["gross_cents"], 20_000)
        self.assertEqual(facts[1]["tax_cents"], 2_000)
        self.assertIn("WITHHELD", facts[1]["statuses"])


class TestStatementImportUI(StatementFixture):
    """离屏 UI 冒烟：预览、确认导入走后台线程，主线程始终保持响应。"""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from desktop.qt import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        super().setUp()
        from data.db import open_database
        from desktop.window import MainWindow

        self.conn = open_database(Path(self.tmp) / "longledger.db")
        self.window = MainWindow(self.conn)
        # 阻断名称补全的网络请求：测试只关心导入链路
        self.window._names_requested.update({"600000", "000001", "510300"})

    def tearDown(self) -> None:
        deadline = time.time() + 3
        while getattr(self.window, "_tasks", []) and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.close()
        self.app.processEvents()
        self.conn.close()
        super().tearDown()

    def test_quote_sync_button_in_header(self) -> None:
        """页面头提供「行情同步」，与「刷新」并列且始终可用（口径只影响公司行为）。"""
        button = self.window.quote_sync_button
        self.assertEqual(button.text(), "行情同步")
        self.assertTrue(button.isEnabled())

    def test_statement_pick_button_becomes_update_after_import(self) -> None:
        """首次导入后「选择文件」变为「更新文件」，作为增量更新入口。"""
        button = self.window.statement_pick_button
        self.assertEqual(button.text(), "选择文件")

        self.conn.execute(
            "UPDATE account SET ledger_mode = 'STATEMENT' WHERE account_id = 1"
        )
        self.conn.commit()
        self.window._reload_all()
        self.assertEqual(button.text(), "选择文件")

        self.window.service.import_trades(
            [
                {
                    "side": "BUY",
                    "symbol": "600000",
                    "trade_date": date(2024, 3, 1),
                    "qty": 100,
                    "price_mills": 10_000,
                    "amount_cents": -100_500,
                }
            ],
            [
                {
                    "entry_date": date(2024, 2, 28),
                    "amount_cents": 2_000_000,
                    "kind": "DEPOSIT",
                    "memo": "银行转证券",
                }
            ],
        )
        self.window._reload_all()
        self.assertEqual(button.text(), "更新文件")

    def test_cash_only_statement_can_be_imported(self) -> None:
        """只有逆回购 / 利息等现金流水的新交割单也能增量入账，不再被「无成交」拦下。"""
        import desktop.pages as pages

        self.conn.execute(
            "UPDATE account SET ledger_mode = 'STATEMENT' WHERE account_id = 1"
        )
        self.conn.commit()
        self.window._reload_all()

        path = Path(self.tmp) / "只有逆回购.txt"
        path.write_text(
            "证券代码,证券名称,操作,成交数量,成交均价,发生金额,资金余额,成交日期\n"
            ",,通用回购逆回 R-001,0,0.000,-1000.02,18999.98,20261109\n"
            ",,通用回购逆回 R-001,0,0.000,1000.08,20001.04,20261110\n",
            encoding="utf-8",
        )
        result = parse_statement(path)
        self.assertEqual(result.trades, [])
        self.assertEqual(len(result.cash_flows), 1)

        self.window._on_statement_parsed(result)
        self.assertTrue(self.window.statement_confirm.isEnabled())
        self.assertIn("现金流水待导入", self.window.statement_count.text())
        # 只有现金流水时，预览也必须列出这笔利息，不能是一片空白
        self.assertEqual(self.window.statement_model.rowCount(), 1)
        row = self.window.statement_model.rows()[0].values
        self.assertEqual(row[4], "利息归本（流入）")
        self.assertEqual(row[8], "0.06")

        original = pages.confirm
        pages.confirm = lambda *args, **kwargs: True
        try:
            self.window._confirm_statement_import()
        finally:
            pages.confirm = original

        self.assertTrue(
            _wait_until(
                lambda: any(
                    entry.kind == "STATEMENT_INTEREST"
                    for entry in self.window.service.portfolio.ledger
                )
            )
        )
        self.assertIn("现金流水", self.window.statement_status.text())

    def test_navigation_and_preview(self) -> None:
        from desktop.window import NAV_ITEMS

        self.assertIn("交割单导入", [item[0] for item in NAV_ITEMS])
        self.assertEqual(self.window.stack.count(), len(NAV_ITEMS))

        result = parse_statement(self.path_a)
        self.window._on_statement_parsed(result)

        self.assertEqual(self.window.statement_model.rowCount(), 4)   # 3 笔成交 + 1 笔红利
        self.assertTrue(self.window.statement_confirm.isEnabled())
        self.assertIn("3 笔成交", self.window.statement_count.text())
        self.assertIn("1 笔现金流水", self.window.statement_count.text())

        first = self.window.statement_model.rows()[0].values
        self.assertEqual(first[1], "2024-01-02")
        self.assertEqual(first[2], "600000")
        self.assertEqual(first[4], "买入")
        self.assertEqual(first[7], "5.00")

        # 现金流水同样进入预览，避免「只看到确认导入按钮亮了」的困惑
        dividend = self.window.statement_model.rows()[2].values
        self.assertEqual(dividend[1], "2024-01-08")
        self.assertEqual(dividend[4], "红利入账（流入）")
        self.assertEqual(dividend[8], "420.00")

    def test_confirm_import_updates_ledger(self) -> None:
        import desktop.pages as pages

        result = parse_statement(self.path_a)
        self.window._on_statement_parsed(result)

        original = pages.confirm
        pages.confirm = lambda *args, **kwargs: True
        try:
            self.window._confirm_statement_import()
        finally:
            pages.confirm = original

        # 确认后预览立即清空，且后台写入在事件循环中完成
        self.assertTrue(
            _wait_until(
                lambda: (
                    len(self.window.service.repo.list_trades(limit=1000)) >= 3
                    and self.window.statement_model.rowCount() == 0
                ),
                timeout=5,
            )
        )
        self.app.processEvents()
        self.assertEqual(self.window.statement_model.rowCount(), 0)
        self.assertFalse(self.window.statement_confirm.isEnabled())

        trades = self.window.service.repo.list_trades(limit=1000)
        self.assertEqual(len(trades), 3)
        self.assertEqual(self.window.service.portfolio.book("600000").total_qty, 500)

    def test_run_task_does_not_block_main_thread(self) -> None:
        done: list = []
        started = time.perf_counter()
        self.window._run_task(
            lambda: (time.sleep(0.25), 7)[1],
            done.append,
            done.append,
        )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.15, "后台任务必须立即返回，不能卡住主线程")
        self.assertTrue(_wait_until(lambda: done, timeout=3))
        self.assertEqual(done, [7])


if __name__ == "__main__":
    unittest.main(verbosity=2)
