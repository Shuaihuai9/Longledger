"""左侧导航的维护页：数据导入 / 公司行为 / 账户设置。

本层只做「收集输入 → 调服务 → 展示结果」，账务口径仍由 services / core 决定；
行情下载、分红同步等网络请求放在后台线程执行，避免界面卡死。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from config.settings import ACCOUNT_DATA_DIR, LEDGER_MODE_LABELS, QUOTE_DATA_DIR, FeeConfig
from core.fees import SIDE_BUY, SIDE_SELL
from data.repositories import Repository
from desktop.dialogs import (
    DIVIDEND_STATUS_LABELS,
    ExportRangeDialog,
    ManualDividendDialog,
    confirm,
    warn,
)
from desktop.models import RecordRow, RecordsModel
from desktop.qt import (
    QCheckBox,
    QDate,
    QDesktopServices,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QThread,
    Qt,
    QtSignal,
    QUrl,
    QVBoxLayout,
    QWidget,
)
from desktop.theme import COLORS
from desktop.widgets import PaperDateEdit
from ingest.quotes_download import download_history, split_symbols
from services.accounts import AccountService
from services.corporate_actions_service import CorporateActionService
from services.ledger import LedgerService
from services.quotes_service import get_daily_ohlc, list_quote_coverage
from services.securities_service import ensure_names
from services.statement_import import parse_statement


def _to_date(qdate: QDate) -> date:
    return date(qdate.year(), qdate.month(), qdate.day())


def _price(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def _ratio(value: float) -> str:
    """比例规范化：0.0 → "0"、0.30 → "0.3"，与自动同步的去重键口径一致。"""
    return format(Decimal(str(value)).normalize(), "f")


def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 15, 18, 14)
    layout.setSpacing(9)

    head = QHBoxLayout()
    head.setSpacing(10)
    title_label = QLabel(title)
    title_label.setObjectName("cardTitle")
    head.addWidget(title_label)
    if hint:
        hint_label = QLabel(hint)
        hint_label.setObjectName("cardHint")
        head.addWidget(hint_label)
    head.addStretch(1)
    layout.addLayout(head)
    return card, layout


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


class FinanceTask(QThread):
    """把一个同步函数放到后台线程执行，结果以信号回到 UI 线程。"""

    succeeded = QtSignal(object)
    failed = QtSignal(str)

    def __init__(self, work, parent=None) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:  # noqa: D401 - Qt 线程入口
        try:
            self.succeeded.emit(self._work())
        except Exception as exc:  # 线程内异常必须转成信号，否则界面拿不到原因
            self.failed.emit(str(exc))


class MaintenancePagesMixin:
    """数据导入 / 公司行为 / 账户设置三页的构建与交互。"""

    # ------------------------------------------------------------ 公共小工具
    def _init_tasks(self) -> None:
        self._tasks: list[FinanceTask] = []

    def _run_task(self, work, on_done, on_error, button=None) -> None:
        # 信号必须 QueuedConnection：默认 Auto 对 Python 回调会退化为在后台线程
        # 直接调用（Direct），此时触碰 UI 控件会随机崩溃（0xC0000409）。
        queued = Qt.ConnectionType.QueuedConnection
        task = FinanceTask(work, self)
        self._tasks.append(task)
        if button is not None:
            button.setEnabled(False)
        task.succeeded.connect(on_done, queued)
        task.failed.connect(on_error, queued)
        task.finished.connect(lambda: self._finish_task(task, button), queued)
        task.start()

    def _finish_task(self, task: FinanceTask, button) -> None:
        if button is not None:
            button.setEnabled(True)
        if task in self._tasks:
            self._tasks.remove(task)

    def _set_note(self, label: QLabel, text: str, ok: bool = True) -> None:
        color = COLORS["accent"] if ok else COLORS["up"]
        label.setStyleSheet(f"color: {color}; background: transparent;")
        label.setText(text)

    def _open_data_dir(self, path: Path, note: QLabel) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_note(note, f"目录不可用：{exc}", ok=False)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self._set_note(note, f"无法打开目录：{path}", ok=False)
            return
        self._set_note(note, f"已打开：{path}")

    # ============================================================ 数据导入
    def _build_ingest_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self._build_download_card(), 3)
        top.addWidget(self._build_query_card(), 2)
        layout.addLayout(top)

        result_card, self.download_view, self.download_count = self._table_card(
            "下载结果", "逐个标的独立提交，单个失败不影响其余"
        )
        self.download_model = RecordsModel(
            ("证券代码", "证券名称", "本地行数", "起始日", "结束日", "最新收盘", "状态"),
            right_aligned={2, 5},
        )
        self._prepare_table(self.download_view, self.download_model)
        self.download_count.setText("尚无本地行情")
        layout.addWidget(result_card, 1)
        self._refresh_download_results()
        return page

    def _refresh_download_results(self) -> None:
        """用本地 quotes 表回填下载结果，重启后仍能看到行情覆盖情况。"""
        rows: list[RecordRow] = []
        for row in list_quote_coverage(self.conn):
            close_mills = row["close_mills"]
            rows.append(
                RecordRow(
                    values=[
                        row["symbol"],
                        self.name_of(row["symbol"]),
                        f"{row['bars']:,}",
                        row["start_date"],
                        row["end_date"],
                        f"{close_mills / 1000:.3f}" if close_mills is not None else "—",
                        f"已覆盖至 {row['end_date']}",
                    ],
                    tones={6: "accent"},
                )
            )
        self.download_model.set_rows(rows)
        self.download_count.setText(
            f"本地行情 {len(rows)} 个标的" if rows else "尚无本地行情"
        )

    def _build_download_card(self) -> QFrame:
        card, form = _card("批量下载历史行情", "东财主源 · 腾讯兜底 · 只写不复权收盘价")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)

        self.ingest_symbols = QLineEdit()
        self.ingest_symbols.setPlaceholderText("600000, 510300, sh600519（逗号 / 空格分隔）")
        self.ingest_symbols.setClearButtonEnabled(True)

        self.ingest_start = PaperDateEdit(QDate.currentDate().addYears(-3))
        self.ingest_end = PaperDateEdit(QDate.currentDate())
        for date_edit in (self.ingest_start, self.ingest_end):
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.setCalendarPopup(True)

        for column, (text, widget) in enumerate(
            (("证券代码", self.ingest_symbols), ("开始日期", self.ingest_start), ("结束日期", self.ingest_end))
        ):
            grid.addWidget(_field_label(text), 0, column)
            grid.addWidget(widget, 1, column)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        form.addLayout(grid)

        action = QHBoxLayout()
        action.setSpacing(14)
        self.ingest_button = QPushButton("开始下载")
        self.ingest_button.setObjectName("primaryButton")
        self.ingest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ingest_button.clicked.connect(self._submit_download)
        self.ingest_status = QLabel("")
        self.ingest_status.setObjectName("formStatus")
        self.ingest_status.setWordWrap(True)
        self.open_quote_dir_button = QPushButton("打开行情目录")
        self.open_quote_dir_button.setObjectName("ghostButton")
        self.open_quote_dir_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_quote_dir_button.setToolTip(f"行情库 market.db 所在目录：{QUOTE_DATA_DIR}")
        self.open_quote_dir_button.clicked.connect(
            lambda: self._open_data_dir(QUOTE_DATA_DIR, self.ingest_status)
        )
        action.addWidget(self.ingest_button)
        action.addWidget(self.open_quote_dir_button)
        action.addWidget(self.ingest_status, 1)
        form.addLayout(action)

        hint = QLabel("下载完成后持仓市值自动按最新收盘价重算；重复下载同一天会覆盖旧值。")
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        form.addWidget(hint)
        return card

    def _build_query_card(self) -> QFrame:
        card, form = _card("单日行情查询", "不复权开高低收")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)

        self.query_symbol = QLineEdit()
        self.query_symbol.setPlaceholderText("如 600519")
        self.query_symbol.setClearButtonEnabled(True)

        self.query_date = PaperDateEdit(QDate.currentDate())
        self.query_date.setDisplayFormat("yyyy-MM-dd")
        self.query_date.setCalendarPopup(True)

        grid.addWidget(_field_label("证券代码"), 0, 0)
        grid.addWidget(_field_label("日期"), 0, 1)
        grid.addWidget(self.query_symbol, 1, 0)
        grid.addWidget(self.query_date, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        form.addLayout(grid)

        action = QHBoxLayout()
        action.setSpacing(12)
        self.query_button = QPushButton("查询")
        self.query_button.setObjectName("ghostButton")
        self.query_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.query_button.clicked.connect(self._submit_daily_query)
        action.addWidget(self.query_button)

        self.dividend_button = QPushButton("补录派息…")
        self.dividend_button.setObjectName("ghostButton")
        self.dividend_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dividend_button.setToolTip(
            "行情源缺失时，为上方代码手动补录分红 / 送转（按除权日在册批次落账）"
        )
        self.dividend_button.clicked.connect(self._manual_dividend)
        action.addWidget(self.dividend_button)
        action.addStretch(1)
        form.addLayout(action)

        self.query_status = QLabel("查询结果会显示在这里")
        self.query_status.setObjectName("formStatus")
        self.query_status.setWordWrap(True)
        self.query_status.setMinimumHeight(46)
        form.addWidget(self.query_status, 1)
        return card

    def _submit_download(self) -> None:
        symbols = split_symbols(self.ingest_symbols.text())
        if not symbols:
            self._set_note(self.ingest_status, "请先填写证券代码", ok=False)
            return
        start = _to_date(self.ingest_start.date())
        end = _to_date(self.ingest_end.date())
        if start > end:
            self._set_note(self.ingest_status, "开始日期不能晚于结束日期", ok=False)
            return

        self._set_note(self.ingest_status, f"正在下载 {len(symbols)} 个标的，请稍候 …")
        conn, account_id = self.conn, self.account_id
        statement_mode = self.service.is_statement_mode()

        def work():
            results = download_history(Repository(conn, account_id), symbols, start, end)
            # 行情落库后顺带同步分红送转：分红按在册批次落账，失败不阻断行情
            action_total = 0
            action_errors: list[str] = []
            if not statement_mode:
                service = CorporateActionService(conn, account_id)
                for item in results:
                    if item.error:
                        continue
                    try:
                        sync = service.fetch_and_sync_dividends(item.symbol, until=end)
                        action_total += sync.applied
                        if sync.errors:
                            action_errors.append(f"{item.symbol}：{sync.errors[0]}")
                    except Exception as exc:
                        action_errors.append(f"{item.symbol}：{exc}")
            # 下载结果里的代码未必出现在账簿事件里，这里单独补全名称缓存
            missing = [
                item.symbol for item in results
                if not item.error and self.name_of(item.symbol) == "—"
            ]
            resolved = ensure_names(conn, missing, timeout=8) if missing else {}
            return results, resolved, action_total, action_errors

        self._run_task(
            work,
            self._on_download_done,
            lambda msg: self._set_note(self.ingest_status, f"下载失败：{msg}", ok=False),
            self.ingest_button,
        )

    def _on_download_done(self, payload) -> None:
        results, resolved, action_total, action_errors = payload
        if resolved:
            self.names.update(resolved)
        failures = [result for result in results if result.error]
        succeeded = len(results) - len(failures)
        self._refresh_download_results()
        self.download_count.setText(
            f"本次 {succeeded}/{len(results)} 成功 · "
            f"本地 {self.download_model.rowCount()} 个标的"
        )
        message = f"共 {len(results)} 个标的，{succeeded} 个写入成功。"
        if action_total:
            message += f" 同步分红送转 {action_total} 条。"
        if action_errors:
            message += f" 公司行为部分失败：{'；'.join(action_errors[:3])}"
        if failures:
            message += " 失败：" + "；".join(
                f"{result.symbol} {result.error}" for result in failures[:3]
            )
        self._set_note(self.ingest_status, message, ok=succeeded > 0)
        if succeeded:
            self._reload_all()

    def _submit_daily_query(self) -> None:
        ticker = self.query_symbol.text().strip()
        if not ticker:
            self._set_note(self.query_status, "请先填写证券代码", ok=False)
            return
        day = _to_date(self.query_date.date())
        self._set_note(self.query_status, "正在获取行情 …")

        def work():
            return get_daily_ohlc(ticker, day, timeout=10)

        self._run_task(
            work,
            self._on_daily_done,
            lambda msg: self._set_note(self.query_status, f"查询失败：{msg}", ok=False),
            self.query_button,
        )

    def _on_daily_done(self, quote) -> None:
        if not quote.ok:
            self._set_note(
                self.query_status, f"{quote.ticker} 未取到行情：{quote.error}", ok=False
            )
            return
        actual = quote.price_date.isoformat()
        note = "" if quote.is_exact_date else "（该日无成交，已回退到最近交易日）"
        self._set_note(
            self.query_status,
            f"{quote.ticker} {self.name_of(quote.ticker)} · {actual}{note}\n"
            f"前收 {_price(quote.prev_close)}　开 {_price(quote.open)}　"
            f"高 {_price(quote.high)}　低 {_price(quote.low)}　收 {_price(quote.close)}",
        )

    # ------------------------------------------------------------ 补录派息
    def _manual_dividend(self) -> None:
        """为上方代码补录一条分红 / 送转，兜底行情源缺失（多为 ETF）。"""
        symbol = self.query_symbol.text().strip()
        if not symbol:
            self._set_note(self.query_status, "请先填写证券代码，再补录派息", ok=False)
            return
        dialog = ManualDividendDialog(symbol, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if (
            Decimal(values["cash_per_share_yuan"]) <= 0
            and Decimal(values["bonus_ratio"]) <= 0
            and Decimal(values["transfer_ratio"]) <= 0
        ):
            self._set_note(self.query_status, "补录失败：每股派息、送股、转增至少填写一项。", ok=False)
            return
        try:
            CorporateActionService(
                self.conn, self.account_id
            ).record_manual_dividend(symbol, **values)
        except Exception as exc:  # 行情缺失、除权日无在册批次等
            self._set_note(self.query_status, f"补录失败：{exc}", ok=False)
            return
        self._set_note(
            self.query_status,
            f"{symbol} 已补录公司行为，批次成本与红利税已重算；可在持仓明细的标的明细里核对。",
        )
        self._reload_all()

    # ============================================================ 账户设置
    def _build_account_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_accounts_card())
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self._build_capital_card(), 1)
        top.addWidget(self._build_fee_card(), 2)
        layout.addLayout(top)
        layout.addWidget(self._build_export_card())
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------ 记录导出
    def _build_export_card(self) -> QFrame:
        card, form = _card(
            "记录导出",
            "买卖明细（含决策记录）与公司行为一次导出为 Excel 工作簿",
        )
        row = QHBoxLayout()
        row.setSpacing(12)
        self.export_button = QPushButton("导出记录…")
        self.export_button.setObjectName("ghostButton")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(self._export_records)
        row.addWidget(self.export_button)

        self.export_status = QLabel(
            "导出两个工作表：「买卖明细」一笔成交一行，含批次块（批次 / 原始成本 / 批次摊薄成本 / "
            "累计分红 / 红利税 / 红利状态）与决策记录；「公司行为」按除权日排序并保留批次明细。"
        )
        self.export_status.setObjectName("formStatus")
        self.export_status.setWordWrap(True)
        row.addWidget(self.export_status, 1)
        form.addLayout(row)
        return card

    def _export_records(self) -> None:
        trades = sorted(
            self.service.portfolio.trades,
            key=lambda t: (t.trade_date, t.seq, t.trade_id),
        )
        first_day = trades[0].trade_date if trades else date.today()
        dialog = ExportRangeDialog(first_day, date.today(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start, end = dialog.values()
        if start > end:
            self._set_note(self.export_status, "开始日期不能晚于结束日期。", ok=False)
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出记录",
            f"长衡记录_{end:%Y%m%d}.xlsx",
            "Excel 工作簿 (*.xlsx)",
        )
        if not path:
            return
        try:
            trade_count, action_count = self._write_export(path, start, end)
        except ImportError:
            self._set_note(
                self.export_status, "导出失败：缺少 openpyxl，请先安装后再试。", ok=False
            )
            return
        except Exception as exc:
            self._set_note(self.export_status, f"导出失败：{exc}", ok=False)
            return
        self._set_note(
            self.export_status,
            f"已导出 {trade_count} 笔买卖、{action_count} 条公司行为：{path}",
        )

    def _write_export(self, path: str, start: date, end: date) -> tuple[int, int]:
        """写出两个工作表：买卖明细（含决策记录）、公司行为（按除权日排序）。"""
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        bold = Font(bold=True)

        # 批次信息按标的预取：买入取成交对应的批次，卖出按标的单独编号
        lots_by_trade: dict[int, object] = {}
        facts_by_symbol: dict[str, dict] = {}
        for symbol, book in self.service.portfolio.books.items():
            for lot in book.lots:
                if lot.open_trade_id:
                    lots_by_trade[lot.open_trade_id] = lot
            facts_by_symbol[symbol] = self.service.dividend_facts(symbol)

        ordered = sorted(
            self.service.portfolio.trades,
            key=lambda t: (t.trade_date, t.seq, t.trade_id),
        )
        sell_numbers: dict[int, int] = {}
        counters: dict[str, int] = {}
        for trade in ordered:
            if trade.side != SIDE_SELL:
                continue
            counters[trade.symbol] = counters.get(trade.symbol, 0) + 1
            sell_numbers[trade.trade_id] = counters[trade.symbol]

        def status_text(fact: dict | None) -> str:
            if not fact:
                return ""
            return " / ".join(
                DIVIDEND_STATUS_LABELS.get(str(status), str(status))
                for status in fact["statuses"]
            )

        # ---- 工作表一：买卖明细 + 决策记录，一笔成交一行，含批次块
        sheet = wb.active
        sheet.title = "买卖明细"
        sheet.append(
            [
                "日期", "证券代码", "证券名称", "方向", "批次", "原始成本(元)",
                "批次摊薄成本(元)", "批次累计分红(元)", "批次红利税(元)", "红利状态",
                "数量", "成交价(元)", "成交额(元)", "佣金(元)", "印花税(元)",
                "过户费(元)", "红利税(元)", "现金净额(元)", "已实现盈亏(元)",
                "决策逻辑", "目标价(元)", "计划内/计划外", "预期持有周期",
                "卖出原因", "是否改变买入逻辑",
            ]
        )
        trade_count = 0
        for trade in ordered:
            if not start <= trade.trade_date <= end:
                continue
            if trade.side == SIDE_SELL:
                batch_label = str(sell_numbers.get(trade.trade_id, "—"))
                original = diluted = gross = batch_tax = None
                status = ""
            else:
                lot = lots_by_trade.get(trade.trade_id)
                fact = (
                    (facts_by_symbol.get(trade.symbol) or {}).get(lot.lot_id)
                    if lot is not None
                    else None
                )
                if lot is None:
                    batch_label = "买入"
                    original = diluted = gross = batch_tax = None
                    status = ""
                else:
                    batch_label = str(lot.lot_id)
                    original = (
                        round(lot.cost_cents / lot.qty_opened / 100, 3)
                        if lot.qty_opened
                        else None
                    )
                    diluted = (
                        round(lot.diluted_cost_mills / 1000, 3)
                        if lot.qty_remaining > 0
                        else None
                    )
                    gross = round(fact["gross_cents"] / 100, 2) if fact else None
                    batch_tax = round(fact["tax_cents"] / 100, 2) if fact else None
                    status = status_text(fact)
            sheet.append(
                [
                    trade.trade_date.isoformat(),
                    trade.symbol,
                    self.name_of(trade.symbol),
                    "买入" if trade.side == SIDE_BUY else "卖出",
                    batch_label,
                    original,
                    diluted,
                    gross,
                    batch_tax,
                    status,
                    int(trade.qty),
                    round(trade.price_mills / 1000, 3),
                    round(trade.gross_cents / 100, 2),
                    round(trade.commission_cents / 100, 2),
                    round(trade.stamp_tax_cents / 100, 2),
                    round(trade.transfer_fee_cents / 100, 2),
                    round(trade.dividend_tax_cents / 100, 2),
                    round(trade.net_cash_cents / 100, 2),
                    (
                        round(trade.realized_pnl_cents / 100, 2)
                        if trade.side == SIDE_SELL
                        else None
                    ),
                    trade.trade_logic,
                    round(trade.target_price_mills / 1000, 3)
                    if trade.target_price_mills
                    else None,
                    trade.plan_type,
                    trade.hold_period,
                    trade.sell_reason,
                    trade.strategy_changed,
                ]
            )
            trade_count += 1

        # ---- 工作表二：公司行为，按除权日排序并保留批次明细
        raw = (
            self.service.statement_corporate_actions(None, batch_level=True)
            if self.service.is_statement_mode()
            else self.service.lot_corporate_actions(None)
        )

        def action_day(row: dict) -> str:
            return str(row["除权日"])[:10]

        actions = [
            row for row in raw if start <= date.fromisoformat(action_day(row)) <= end
        ]
        actions.sort(key=lambda row: (action_day(row), str(row["代码"]), str(row["批次"])))

        action_sheet = wb.create_sheet("公司行为")
        action_sheet.append(
            [
                "证券代码", "证券名称", "除权日", "批次", "类型", "在册股数",
                "每股派息(元)", "每股送股", "每股转增", "本批次红利(元)", "红利状态",
            ]
        )
        for row in actions:
            action_sheet.append(
                [
                    str(row["代码"]),
                    self.name_of(str(row["代码"])),
                    action_day(row),
                    str(row["批次"]),
                    str(row["类型"]),
                    int(row.get("股数") or 0),
                    round(float(row["每股派息(元)"]), 3),
                    round(float(row["每股送股"]), 3),
                    round(float(row["每股转增"]), 3),
                    round(float(row["本批次红利(元)"]), 2),
                    DIVIDEND_STATUS_LABELS.get(
                        str(row.get("红利状态", "")), str(row.get("红利状态", ""))
                    ),
                ]
            )

        for target in (sheet, action_sheet):
            target.freeze_panes = "A2"
            for cell in target[1]:
                cell.font = bold
            for column, cells in enumerate(target.columns, start=1):
                width = max(len(str(cell.value or "")) for cell in cells)
                target.column_dimensions[get_column_letter(column)].width = min(
                    max(width + 2, 8), 42
                )

        wb.save(path)
        return trade_count, len(actions)

    def _build_accounts_card(self) -> QFrame:
        card, form = _card("账户管理", "新建 / 删除账户；删除会连同该账户全部流水一起移除")

        row = QHBoxLayout()
        row.setSpacing(10)
        self.current_account_label = QLabel("")
        self.current_account_label.setObjectName("cardHint")
        self.add_account_button = QPushButton("新建")
        self.remove_account_button = QPushButton("删除")
        for button in (self.add_account_button, self.remove_account_button):
            button.setObjectName("ghostButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_account_button.clicked.connect(self._add_account)
        self.remove_account_button.clicked.connect(self._remove_account)
        self.open_account_dir_button = QPushButton("打开账户目录")
        self.open_account_dir_button.setObjectName("ghostButton")
        self.open_account_dir_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_account_dir_button.setToolTip(
            f"账户库 longledger.db 所在目录：{ACCOUNT_DATA_DIR}"
        )
        self.open_account_dir_button.clicked.connect(
            lambda: self._open_data_dir(ACCOUNT_DATA_DIR, self.account_status)
        )
        row.addWidget(self.current_account_label, 1)
        row.addWidget(self.open_account_dir_button)
        row.addWidget(self.add_account_button)
        row.addWidget(self.remove_account_button)
        form.addLayout(row)

        self.account_status = QLabel("删除账户不可恢复，已有成交的账户建议先留档。")
        self.account_status.setObjectName("formStatus")
        self.account_status.setWordWrap(True)
        form.addWidget(self.account_status)
        return card

    def _build_capital_card(self) -> QFrame:
        card, form = _card("账户本金", "初始本金 / 出入金")

        self.capital_info = QLabel("")
        self.capital_info.setObjectName("cardHint")
        self.capital_info.setWordWrap(True)
        form.addWidget(self.capital_info)

        self.capital_spin = QDoubleSpinBox()
        self.capital_spin.setDecimals(2)
        self.capital_spin.setRange(0.0, 1_000_000_000.0)
        self.capital_spin.setGroupSeparatorShown(True)
        self.capital_label = _field_label("初始本金 (元)")
        form.addWidget(self.capital_label)
        form.addWidget(self.capital_spin)

        self.capital_button = QPushButton("保存初始本金")
        self.capital_button.setObjectName("ghostButton")
        self.capital_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capital_button.clicked.connect(self._save_initial_capital)
        form.addWidget(self.capital_button)

        self.cash_spin = QDoubleSpinBox()
        self.cash_spin.setDecimals(2)
        self.cash_spin.setRange(0.0, 1_000_000_000.0)
        self.cash_spin.setGroupSeparatorShown(True)
        form.addWidget(_field_label("出入金金额 (元)"))
        form.addWidget(self.cash_spin)

        cash_row = QHBoxLayout()
        cash_row.setSpacing(10)
        self.deposit_button = QPushButton("入金")
        self.withdraw_button = QPushButton("出金")
        for button in (self.deposit_button, self.withdraw_button):
            button.setObjectName("ghostButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            cash_row.addWidget(button)
        self.deposit_button.clicked.connect(lambda _=False: self._move_cash(True))
        self.withdraw_button.clicked.connect(lambda _=False: self._move_cash(False))
        form.addLayout(cash_row)

        self.capital_status = QLabel("已有成交时，改初始本金只调整净值基准，不动现金")
        self.capital_status.setObjectName("formStatus")
        self.capital_status.setWordWrap(True)
        form.addWidget(self.capital_status, 1)
        return card

    def _build_fee_card(self) -> QFrame:
        card, form = _card("交易参数", "全账户共享 · 只影响后续成交")

        self.fee_commission = QDoubleSpinBox()
        self.fee_commission.setDecimals(4)
        self.fee_commission.setRange(0.0, 100.0)
        self.fee_commission.setSingleStep(0.1)

        self.fee_min_commission = QDoubleSpinBox()
        self.fee_min_commission.setDecimals(2)
        self.fee_min_commission.setRange(0.0, 1000.0)
        self.fee_min_commission.setSingleStep(1.0)

        self.fee_stamp = QDoubleSpinBox()
        self.fee_stamp.setDecimals(4)
        self.fee_stamp.setRange(0.0, 10.0)
        self.fee_stamp.setSingleStep(0.1)

        self.fee_transfer = QDoubleSpinBox()
        self.fee_transfer.setDecimals(4)
        self.fee_transfer.setRange(0.0, 100.0)
        self.fee_transfer.setSingleStep(0.1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        fields = (
            ("佣金费率 (万分之)", self.fee_commission),
            ("最低佣金 (元)", self.fee_min_commission),
            ("印花税率 (千分之)", self.fee_stamp),
            ("过户费率 (十万分之)", self.fee_transfer),
        )
        for index, (text, widget) in enumerate(fields):
            row, column = divmod(index, 2)
            grid.addWidget(_field_label(text), row * 2, column)
            grid.addWidget(widget, row * 2 + 1, column)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        form.addLayout(grid)

        checks = QHBoxLayout()
        checks.setSpacing(18)
        self.fee_etf_stamp = QCheckBox("ETF 卖出免印花税")
        self.fee_etf_transfer = QCheckBox("ETF 免过户费")
        checks.addWidget(self.fee_etf_stamp)
        checks.addWidget(self.fee_etf_transfer)
        checks.addStretch(1)
        form.addLayout(checks)

        action = QHBoxLayout()
        action.setSpacing(14)
        self.fee_button = QPushButton("保存交易参数")
        self.fee_button.setObjectName("primaryButton")
        self.fee_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fee_button.clicked.connect(self._save_fee_config)
        self.fee_status = QLabel("")
        self.fee_status.setObjectName("formStatus")
        self.fee_status.setWordWrap(True)
        action.addWidget(self.fee_button)
        action.addWidget(self.fee_status, 1)
        form.addLayout(action)
        return card

    def _save_initial_capital(self) -> None:
        cents = int(round(self.capital_spin.value() * 100))
        try:
            self.service.set_initial_capital(cents)
        except Exception as exc:
            self._set_note(self.capital_status, f"未保存：{exc}", ok=False)
            return
        label = "期初现金" if self.service.is_statement_mode() else "初始本金"
        self._set_note(self.capital_status, f"{label}已保存。")
        self._reload_all()

    def _move_cash(self, deposit: bool) -> None:
        cents = int(round(self.cash_spin.value() * 100))
        label = "入金" if deposit else "出金"
        try:
            if deposit:
                self.service.deposit(cents)
            else:
                self.service.withdraw(cents)
        except Exception as exc:
            self._set_note(self.capital_status, f"{label}未完成：{exc}", ok=False)
            return
        self._set_note(self.capital_status, f"已{label} {cents / 100:,.2f} 元。")
        self.cash_spin.setValue(0.0)
        self._reload_all()

    def _save_fee_config(self) -> None:
        config = FeeConfig(
            commission_rate=Decimal(str(self.fee_commission.value())) / Decimal("10000"),
            min_commission_cents=int(round(self.fee_min_commission.value() * 100)),
            stamp_tax_rate=Decimal(str(self.fee_stamp.value())) / Decimal("1000"),
            transfer_fee_rate=Decimal(str(self.fee_transfer.value())) / Decimal("100000"),
            etf_stamp_tax_exempt=self.fee_etf_stamp.isChecked(),
            etf_transfer_fee_exempt=self.fee_etf_transfer.isChecked(),
        )
        try:
            AccountService(self.conn).update_fee_config(config)
        except Exception as exc:
            self._set_note(self.fee_status, f"未保存：{exc}", ok=False)
            return
        self._set_note(self.fee_status, "交易参数已保存，仅影响后续成交。")
        self._reload_all()

    def _refresh_account_fields(self) -> None:
        current = next(
            (
                row["name"]
                for row in getattr(self, "account_rows", [])
                if int(row["account_id"]) == self.account_id
            ),
            "—",
        )
        mode = self.service.repo.account_ledger_mode()
        mode_label = LEDGER_MODE_LABELS.get(mode, mode)
        self.current_account_label.setText(f"当前账户：{current} · {mode_label}")

        initial = self.service.initial_capital_cents() / 100
        cash = self.service.portfolio.cash_cents / 100
        statement = self.service.is_statement_mode()
        if statement:
            self.capital_info.setText(
                f"当前期初现金 {initial:,.2f} 元 · 可用现金 {cash:,.2f} 元"
                " · 费率与红利均以导入的交割单为准"
            )
        else:
            self.capital_info.setText(
                f"当前初始本金 {initial:,.2f} 元 · 可用现金 {cash:,.2f} 元"
            )
        self.capital_label.setText("期初现金 (元)" if statement else "初始本金 (元)")
        self.capital_button.setText("保存期初现金" if statement else "保存初始本金")

        self.capital_spin.blockSignals(True)
        self.capital_spin.setValue(initial)
        self.capital_spin.blockSignals(False)

        config = AccountService(self.conn).current_fee_config()
        pairs = (
            (self.fee_commission, float(config.commission_rate) * 10000),
            (self.fee_min_commission, config.min_commission_cents / 100),
            (self.fee_stamp, float(config.stamp_tax_rate) * 1000),
            (self.fee_transfer, float(config.transfer_fee_rate) * 100000),
        )
        for spin, value in pairs:
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        for checkbox, checked in (
            (self.fee_etf_stamp, config.etf_stamp_tax_exempt),
            (self.fee_etf_transfer, config.etf_transfer_fee_exempt),
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)

    # ============================================================ 交割单导入
    _STATEMENT_FIELD_LABELS = {
        "ticker": "证券代码",
        "name": "证券名称",
        "date": "成交日期",
        "action": "买卖方向",
        "qty": "成交数量",
        "price": "成交价格",
        "commission": "佣金",
        "stamp": "印花税",
        "transfer": "过户费",
        "other_fee": "其他杂费",
        "amount": "发生金额",
        "balance": "资金余额",
    }

    def _build_statement_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_statement_pick_card())

        preview_card, self.statement_view, self.statement_count = self._table_card(
            "清洗预览", "成交、现金流水与股数入账都会列出 · 确认后才会写入账本"
        )
        self.statement_model = RecordsModel(
            (
                "来源行",
                "成交日期",
                "证券代码",
                "证券名称",
                "方向",
                "成交价",
                "数量",
                "佣金(元)",
                "金额(元)",
            ),
            right_aligned={0, 5, 6, 7, 8},
        )
        self._prepare_table(self.statement_view, self.statement_model)
        # 预览默认按成交日期升序，而不是按原始行号
        self.statement_view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.statement_count.setText("尚未选择文件")
        layout.addWidget(preview_card, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.statement_status = QLabel("请选择一份券商导出的历史交割单。")
        self.statement_status.setObjectName("formStatus")
        self.statement_status.setWordWrap(True)
        self.statement_confirm = QPushButton("确认导入")
        self.statement_confirm.setObjectName("primaryButton")
        self.statement_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.statement_confirm.setEnabled(False)
        self.statement_confirm.clicked.connect(self._confirm_statement_import)
        bottom.addWidget(self.statement_status, 1)
        bottom.addWidget(self.statement_confirm)
        layout.addLayout(bottom)
        return page

    def _build_statement_pick_card(self) -> QFrame:
        card, form = _card("选择交割单文件", "CSV / Excel · UTF-8 / GBK · 自动定位表头")

        row = QHBoxLayout()
        row.setSpacing(14)
        self.statement_pick_button = QPushButton("选择文件")
        self.statement_pick_button.setObjectName("primaryButton")
        self.statement_pick_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.statement_pick_button.clicked.connect(self._pick_statement_file)
        self.statement_path_label = QLabel("尚未选择文件")
        self.statement_path_label.setObjectName("cardHint")
        self.statement_path_label.setWordWrap(True)
        row.addWidget(self.statement_pick_button)
        row.addWidget(self.statement_path_label, 1)
        form.addLayout(row)

        hint = QLabel(
            "系统按列名关键词自动识别证券代码、成交日期、买卖方向、数量、价格、佣金、"
            "印花税、过户费与「发生金额」，并丢弃流水号、委托号、资金余额等冗余列。"
            "系统计算口径会按「账户设置」的交易参数重算费用，红利/利息按发生金额补入现金；"
            "交割单口径则完全采用交割单原值，佣金、税费、红利与银行流水按发生金额入账。"
            "两种口径下，银行入金都按流水入账，逆回购本金视为资金停放（仍计入可用资金），"
            "只有回购到期利息计入收益。"
            "首次导入后按钮会变成「更新文件」：再选新的周 / 月度交割单即可增量更新，"
            "重复记录自动跳过，只有现金流水的文件也能单独导入。"
        )
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        form.addWidget(hint)
        return card

    def _has_imported_statement(self) -> bool:
        """已导入过交割单：有成交，或已有交割单产生的现金流水。"""
        if self.service.repo.list_trades(limit=1):
            return True
        return any(
            str(row["kind"]).startswith("STATEMENT_")
            for row in self.service.repo.list_cash_entries()
        )

    def _refresh_statement_pick_button(self) -> None:
        """首次导入后把「选择文件」改成「更新文件」，提示可以继续增量更新。"""
        if not hasattr(self, "statement_pick_button"):
            return
        update = self.service.is_statement_mode() and self._has_imported_statement()
        self.statement_pick_button.setText("更新文件" if update else "选择文件")

    def _pick_statement_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择交割单文件",
            "",
            "交割单 (*.csv *.txt *.xls *.xlsx *.xlsm);;CSV (*.csv *.txt);;"
            "Excel (*.xls *.xlsx *.xlsm);;所有文件 (*)",
        )
        if not path:
            return

        # 选新文件时先清空旧预览，避免误把上一次的解析结果写进账本
        self._statement_result = None
        self._statement_balance_cents = None
        self._statement_repo_outstanding = 0
        self.statement_model.set_rows([])
        self.statement_confirm.setEnabled(False)
        self.statement_count.setText("正在解析 …")
        self.statement_path_label.setText(path)
        self.statement_path_label.setToolTip("")
        self._set_note(self.statement_status, "正在解析文件，请稍候 …")

        self._run_task(
            lambda: parse_statement(path),
            self._on_statement_parsed,
            self._on_statement_failed,
            self.statement_pick_button,
        )

    def _on_statement_parsed(self, result) -> None:
        self._statement_result = result
        statement_mode = self.service.is_statement_mode()
        rows = [self._statement_preview_row(trade) for trade in result.trades]
        cash_rows = result.cash_flows if statement_mode else result.income_flows
        rows.extend(self._cash_preview_row(flow) for flow in cash_rows)
        if statement_mode:
            rows.extend(self._share_preview_row(item) for item in result.shares)
        self.statement_model.set_rows(rows)

        mapping_text = " · ".join(
            f"{self._STATEMENT_FIELD_LABELS.get(field_name, field_name)}←{source}"
            for field_name, source in result.mapping.items()
            if source
        )
        self.statement_path_label.setToolTip(mapping_text)

        has_trade_rows = bool(result.trades or (statement_mode and result.shares))
        has_cash_rows = bool(statement_mode and result.cash_flows)
        if not has_trade_rows and not has_cash_rows:
            self.statement_count.setText("无有效成交")
            self.statement_confirm.setEnabled(False)
            message = "文件解析成功，但没有找到可导入的买卖成交。"
            if result.skipped:
                message += f"\n已隔离 / 跳过 {len(result.skipped)} 条非买卖记录。"
            self._set_note(self.statement_status, message, ok=False)
            warn(self, "没有可导入的成交", message)
            return

        missing_amount = len(result.trades) - result.trade_amount_count
        if statement_mode and missing_amount:
            self.statement_count.setText("缺少发生金额")
            self.statement_confirm.setEnabled(False)
            message = (
                f"有 {missing_amount} 笔成交缺少「发生金额」，交割单口径无法入账。\n"
                "请换用含「发生金额」列的交割单，或改用系统计算口径账户。"
            )
            self._set_note(self.statement_status, message, ok=False)
            warn(self, "交割单缺少发生金额", message)
            return

        parts = [f"已解析 {len(result.trades)} 笔成交"] if result.trades else []
        if result.ignored_headers:
            parts.append(f"丢弃 {len(result.ignored_headers)} 个冗余列")
        if result.non_trade_count:
            parts.append(f"隔离 {result.non_trade_count} 条非交易记录")
        if result.invalid_count:
            parts.append(f"跳过 {result.invalid_count} 条无效行")
        if statement_mode:
            if result.cash_flows:
                total = sum(flow.amount_cents for flow in result.cash_flows)
                parts.append(
                    f"按发生金额入账 {len(result.cash_flows)} 笔现金流水"
                    f"，合计 {total / 100:,.2f} 元"
                )
        elif result.cash_flow_count:
            parts.append(
                f"补入 {result.cash_flow_count} 笔红利/利息流水"
                f"（合计 {result.cash_flow_cents / 100:,.2f} 元）"
            )
        if result.repo_outstanding_cents:
            parts.append(
                f"逆回购在途 {result.repo_outstanding_cents / 100:,.2f} 元"
                "视为资金停放（仍计入可用资金）"
            )
        if result.shares and statement_mode:
            parts.append(
                f"另有 {len(result.shares)} 笔红股/送转/新股入账"
                f"（合计 {sum(item.qty for item in result.shares):,} 股，按 0 成本增加持仓）"
            )
        if statement_mode and result.final_balance_cents is not None:
            parts.append(
                f"末行资金余额 {result.final_balance_cents / 100:,.2f} 元，导入后自动对账"
            )
        if result.share_balances:
            parts.append(
                f"股票余额 {len(result.share_balances)} 个标的，导入后核对账面持仓股数"
            )
        cash_count = len(result.cash_flows) if statement_mode else len(result.income_flows)
        count_parts: list[str] = []
        if result.trades:
            count_parts.append(f"{len(result.trades)} 笔成交")
        if result.shares and statement_mode:
            count_parts.append(f"{len(result.shares)} 笔股数入账")
        if cash_count:
            count_parts.append(f"{cash_count} 笔现金流水")
        self.statement_count.setText(" + ".join(count_parts) + "待导入")
        self.statement_confirm.setEnabled(True)
        self._set_note(
            self.statement_status,
            "，".join(parts) + "。请核对预览后确认导入。",
        )

    def _on_statement_failed(self, message: str) -> None:
        self._statement_result = None
        self.statement_model.set_rows([])
        self.statement_count.setText("解析失败")
        self.statement_confirm.setEnabled(False)
        self._set_note(self.statement_status, f"解析失败：{message}", ok=False)
        warn(self, "交割单解析失败", message)

    def _statement_preview_row(self, trade) -> RecordRow:
        is_sell = trade.side == "SELL"
        amount = (
            "—"
            if trade.amount_cents is None
            else f"{trade.amount_cents / 100:,.2f}"
        )
        return RecordRow(
            values=[
                str(trade.source_row),
                trade.trade_date.isoformat(),
                trade.symbol,
                trade.name or "—",
                "卖出" if is_sell else "买入",
                f"{trade.price_mills / 1000:.3f}",
                f"{trade.qty:,}",
                f"{trade.commission_cents / 100:,.2f}",
                amount,
            ],
            tones={4: "up" if is_sell else "accent"},
            payload=trade,
        )

    _CASH_KIND_LABELS = {
        "DEPOSIT": "银行转入",
        "WITHDRAW": "银行转出",
        "STATEMENT_DIVIDEND": "红利入账",
        "STATEMENT_DIVIDEND_TAX": "股息红利税",
        "STATEMENT_INTEREST": "利息归本",
        "STATEMENT_CASH": "现金流水",
    }

    @staticmethod
    def _memo_symbol(memo: str) -> str:
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", memo or "")
        return match.group(0) if match else "—"

    def _cash_preview_row(self, flow) -> RecordRow:
        """现金流水也进预览：用户能直接看到这笔新流水会被记成什么。"""
        label = self._CASH_KIND_LABELS.get(flow.kind, flow.kind)
        inflow = flow.amount_cents >= 0
        return RecordRow(
            values=[
                str(flow.source_row),
                flow.entry_date.isoformat(),
                self._memo_symbol(flow.memo),
                flow.memo or "—",
                f"{label}（{'流入' if inflow else '流出'}）",
                "—",
                "—",
                "—",
                f"{flow.amount_cents / 100:,.2f}",
            ],
            tones={4: "up" if inflow else "down"},
            payload=flow,
        )

    def _share_preview_row(self, item) -> RecordRow:
        """红股 / 送转 / 新股入账：0 成本，只增加股数。"""
        return RecordRow(
            values=[
                str(item.source_row),
                item.entry_date.isoformat(),
                item.symbol,
                item.memo or "—",
                "股数入账",
                "0.000",
                f"{item.qty:,}",
                "—",
                "—",
            ],
            tones={4: "accent"},
            payload=item,
        )

    def _confirm_statement_import(self) -> None:
        result = getattr(self, "_statement_result", None)
        if result is None:
            return
        statement_mode = self.service.is_statement_mode()
        has_trade_rows = bool(result.trades or (statement_mode and result.shares))
        has_cash_rows = bool(statement_mode and result.cash_flows)
        if not has_trade_rows and not has_cash_rows:
            return
        count = len(result.trades)

        if statement_mode:
            if count or result.shares:
                message = (
                    f"将向当前账户写入 {count} 笔买卖成交，成交金额、佣金、印花税与过户费"
                    "完全按交割单原值入账，不再按交易参数重算，红利税也不重算。"
                )
                if result.cash_flows:
                    total = sum(flow.amount_cents for flow in result.cash_flows)
                    message += (
                        f"\n另按发生金额入账 {len(result.cash_flows)} 笔现金流水"
                        f"，合计 {total / 100:,.2f} 元。"
                    )
            else:
                total = sum(flow.amount_cents for flow in result.cash_flows)
                message = (
                    f"将按发生金额向当前账户写入 {len(result.cash_flows)} 笔现金流水"
                    f"，合计 {total / 100:,.2f} 元。"
                )
            message += "\n重复的成交与现金流水会自动跳过。"
            if result.shares:
                message += (
                    f"\n另有 {len(result.shares)} 笔红股/送转/新股入账"
                    f"（合计 {sum(item.qty for item in result.shares):,} 股），按 0 成本增加持仓。"
                )
        else:
            message = (
                f"将向当前账户写入 {count} 笔买卖成交，并按当前交易参数重算佣金、"
                "印花税与过户费，随后重算 FIFO 批次。"
            )
            if result.cash_flow_count:
                message += (
                    f"\n另补入 {result.cash_flow_count} 笔红利/利息等现金流水，"
                    f"合计 {result.cash_flow_cents / 100:,.2f} 元。"
                )
            if result.bank_transfer_cents:
                message += (
                    f"\n银行转账合计 {result.bank_transfer_cents / 100:,.2f} 元，"
                    "视为已含在账户初始本金，不再重复入账。"
                )
        if result.repo_outstanding_cents:
            message += (
                f"\n逆回购在途 {result.repo_outstanding_cents / 100:,.2f} 元，按资金停放处理，"
                "本金仍计入可用资金。"
            )
        if result.repo_interest_cents:
            message += (
                f"\n回购到期利息 {result.repo_interest_cents / 100:,.2f} 元已计入收益。"
            )
        message += "\n是否继续？"
        if not confirm(self, "确认导入交割单", message, "确认导入"):
            return

        payload = []
        for trade in result.trades:
            item = {
                "side": trade.side,
                "symbol": trade.symbol,
                "trade_date": trade.trade_date,
                "qty": trade.qty,
                "price_mills": trade.price_mills,
            }
            if statement_mode:
                item.update(
                    {
                        "amount_cents": trade.amount_cents,
                        "commission_cents": trade.commission_cents,
                        "stamp_tax_cents": trade.stamp_tax_cents,
                        "transfer_fee_cents": trade.transfer_fee_cents,
                        "other_fee_cents": trade.other_fee_cents,
                        "seq": trade.source_row,
                    }
                )
            payload.append(item)
        if statement_mode:
            for share in result.shares:
                payload.append(
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
                )
        self._statement_balance_cents = result.final_balance_cents
        self._statement_share_balances = dict(result.share_balances)
        self._statement_repo_outstanding = result.repo_outstanding_cents
        cash_source = result.cash_flows if statement_mode else result.income_flows
        cash_payload = []
        for flow in cash_source:
            cash_item = {
                "entry_date": flow.entry_date,
                "amount_cents": flow.amount_cents,
                "kind": flow.kind,
                "memo": flow.memo,
                "qty": int(getattr(flow, "qty", 0) or 0),
            }
            if statement_mode:
                cash_item["seq"] = flow.source_row
            cash_payload.append(cash_item)
        conn, account_id = self.conn, self.account_id
        self.statement_confirm.setEnabled(False)
        if count:
            self._set_note(self.statement_status, f"正在写入 {count} 笔成交 …")
        else:
            self._set_note(
                self.statement_status,
                f"正在写入 {len(result.cash_flows)} 笔现金流水 …",
            )

        def work():
            service = LedgerService(conn, account_id)
            count = service.import_trades(payload, cash_payload)
            holdings = {
                symbol: sum(lot.qty_remaining for lot in book.lots)
                for symbol, book in service.portfolio.books.items()
            }
            return (
                count,
                service.last_import_duplicates,
                service.portfolio.cash_cents,
                service.last_import_cash_written,
                holdings,
            )

        # 不把按钮交给 _finish_task 管理：成功后预览应保持清空，不能再次提交
        self._run_task(work, self._on_statement_import_done, self._on_statement_import_failed)

    def _on_statement_import_done(self, result) -> None:
        count, duplicates, cash_cents, cash_written, holdings = (
            result if isinstance(result, tuple) and len(result) == 5
            else (result, 0, None, 0, None)
        )
        self._statement_result = None
        self.statement_model.set_rows([])
        self.statement_count.setText(f"已导入 {count + cash_written} 条记录")
        written: list[str] = []
        if count:
            written.append(f"{count} 条成交 / 股数变动")
        if cash_written:
            written.append(f"{cash_written} 笔现金流水")
        if written:
            text = "已写入 " + "、".join(written) + "，FIFO 批次与现金已自动重算。"
        else:
            text = "没有新增记录，账簿保持不变。"
        if duplicates:
            text += f"跳过 {duplicates} 条已存在的成交 / 现金流水。"
        balance = getattr(self, "_statement_balance_cents", None)
        outstanding = getattr(self, "_statement_repo_outstanding", 0)
        if (
            self.service.is_statement_mode()
            and balance is not None
            and cash_cents is not None
        ):
            # 券商「资金余额」已扣除逆回购冻结本金，本软件按资金停放仍计入现金
            diff = cash_cents - (balance + outstanding)
            if diff == 0:
                text += (
                    f"\n对账通过：账面现金与交割单末行资金余额一致"
                    f"（{balance / 100:,.2f} 元）。"
                )
            else:
                text += (
                    f"\n对账差异 {diff / 100:,.2f} 元：账面现金 {cash_cents / 100:,.2f} 元，"
                    f"交割单末行资金余额 {balance / 100:,.2f} 元；"
                    "请核对期初现金与是否已全量导入。"
                )
        share_balances = getattr(self, "_statement_share_balances", {})
        if self.service.is_statement_mode() and share_balances and holdings is not None:
            diffs = [
                f"{symbol} 账面 {int(holdings.get(symbol, 0)):,} 股 / "
                f"交割单 {int(qty):,} 股"
                for symbol, qty in share_balances.items()
                if int(holdings.get(symbol, 0)) != int(qty)
            ]
            if diffs:
                text += (
                    "\n持仓对账差异：" + "；".join(diffs[:5])
                    + "。账本可能缺少更早的成交，建议删除该账户后全量重新导入。"
                )
            else:
                text += (
                    f"\n持仓对账通过：{len(share_balances)} 个标的的账面股数"
                    "与交割单「股票余额」一致。"
                )
        self._set_note(self.statement_status, text)
        self._reload_all()

    def _on_statement_import_failed(self, message: str) -> None:
        self.statement_confirm.setEnabled(True)
        hint = ""
        if "现金不足" in message and self.service.is_statement_mode():
            hint = (
                "\n\n请确认期初现金与交割单起始余额一致；若确实从 0 起步，"
                "请补入更早的交割单（银行入金与红利流水也会按发生金额入账）。"
            )
        elif "现金不足" in message:
            hint = (
                "\n\n若确认初始本金与交割单一致，请到「账户设置」核对佣金费率与最低佣金："
                "系统默认万 2.5 / 最低 5 元，高于多数券商的万 0.3~万 0.5 / 最低 1 元，"
                "费用多算会把现金提前耗尽。"
            )
        self._set_note(self.statement_status, f"导入失败：{message}", ok=False)
        warn(self, "交割单导入失败", message + hint)
