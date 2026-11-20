"""主窗口：纸质账簿风的持仓/交易桌面端。

本层只做「展示 + 调用服务」，所有账务口径来自 services / core，未改动任何后端逻辑。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from config.settings import APP_VERSION
from core.fees import SIDE_BUY, SIDE_SELL
from desktop.dialogs import (
    NewAccountDialog,
    SymbolDetailDialog,
    TradeEditDialog,
    TradePlanDialog,
    confirm,
    warn,
)
from desktop.models import (
    HoldingRow,
    HoldingsModel,
    RecordsModel,
    trade_record_row,
)
from desktop.qt import (
    QAbstractItemView,
    QButtonGroup,
    QDate,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPixmap,
    QPushButton,
    QSettings,
    QSpinBox,
    QStackedWidget,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from desktop.pages import MaintenancePagesMixin
from desktop.symbols import display_name
from desktop.theme import COLORS, TABLE_FONT_PX
from desktop.widgets import (
    HistoryLineEdit,
    MetricCard,
    PaperComboBox,
    PaperTableView,
    YearTailDateEdit,
)
from data.repositories import Repository
from ingest.quotes_download import download_history
from services.accounts import AccountService
from services.corporate_actions_service import CorporateActionService
from services.ledger import (
    LedgerService,
    migrate_dividend_exempt,
    migrate_lot_dividend,
    migrate_realized_avg_cost,
)
from services.quotes_service import get_daily_ohlc
from services.securities_service import cached_names, ensure_names

# (导航名, 页面标题, 副标题, 分组)
NAV_ITEMS = (
    ("买卖", "买卖 · 记一笔成交", "费用按券商参数实时计算，批次进入 FIFO 队列", "账簿"),
    ("持仓明细", "持仓明细", "", "账簿"),
    ("数据导入", "数据导入", "批量补全日线收盘价，或查询单日不复权行情", "数据与维护"),
    ("交割单导入", "交割单导入", "导入券商历史交割单 · 智能识别列名 · 隔离非交易记录", "数据与维护"),
    ("账户设置", "账户设置", "账户管理、初始本金、出入金与全账户共享的交易费率", "数据与维护"),
)

HOLDINGS_PAGE = 1
INGEST_PAGE = 3

# 品牌标识：主标「墨斗」，副标「白文印」（assets/brand 下）
BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"
APP_MARK = BRAND_DIR / "changheng-mark-inkline-line.jpg"


def _money(value: float) -> str:
    return f"¥{value:,.2f}"


class MainWindow(MaintenancePagesMixin, QMainWindow):
    def __init__(self, conn) -> None:
        super().__init__()
        self.conn = conn
        self.settings = QSettings("LongLedger", "Desktop")
        self._table_views: list[PaperTableView] = []

        # 旧口径红利状态 / 已实现盈亏一次性迁移（全量重放，成功后写 meta 标记）
        self._migration_note = ""
        try:
            migrate_dividend_exempt(conn)
            migrate_realized_avg_cost(conn)
            migrate_lot_dividend(conn)
        except Exception as exc:
            self._migration_note = f"账务口径迁移未完成：{exc}"

        self.account_rows = AccountService(conn).list_accounts_with_summary()
        self.account_id = (
            int(self.account_rows[0]["account_id"]) if self.account_rows else 1
        )
        self.service = LedgerService(conn, self.account_id)

        # 名称缓存：静态表 -> meta 表；缺失的后台补全，本会话内只请求一次
        self.names: dict[str, str] = cached_names(conn)
        self._names_requested: set[str] = set()
        self._detail_dialogs: list[SymbolDetailDialog] = []
        self._ledger_busy = False  # 有成交/重算在后台进行时，屏蔽新的账务操作
        self._statement_mode = False  # 交割单口径：手动记账入口全部停用

        self.trade_inputs: dict[str, QWidget] = {}
        self.quote_data: dict = {}
        self._init_tasks()

        self._build_ui()
        self._restore_geometry()
        self._reload_all()

        # 动态刷新：红利免税与否随持股期限只增不减，跨过「满 1 年」临界点后
        # 需自动翻转状态，而无需用户手动刷新。每分钟重读一次本地的账务状态。
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(60_000)
        self._status_timer.timeout.connect(self._reload_all)
        self._status_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.setWindowTitle("长衡")
        self.resize(1440, 900)
        self.setMinimumSize(1240, 760)
        if APP_MARK.exists():
            self.setWindowIcon(QIcon(str(APP_MARK)))

        root = QWidget()
        root.setObjectName("Root")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 22, 30, 24)
        content_layout.setSpacing(16)
        content_layout.addLayout(self._build_header())
        content_layout.addLayout(self._build_overview())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_trade_page())
        self.stack.addWidget(self._build_holdings_page())
        self.stack.addWidget(self._build_ingest_page())
        self.stack.addWidget(self._build_statement_page())
        self.stack.addWidget(self._build_account_page())
        content_layout.addWidget(self.stack, 1)

        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)
        self._refresh_accounts(self.account_id)
        self._switch_page(HOLDINGS_PAGE)

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(214)

        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 26, 0, 18)
        layout.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(22, 0, 22, 0)
        brand.setSpacing(10)
        if APP_MARK.exists():
            mark = QLabel()
            mark.setPixmap(
                QPixmap(str(APP_MARK)).scaled(
                    40,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            mark.setFixedSize(40, 40)
            brand.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)
        title = QLabel("长衡")
        title.setObjectName("brandTitle")
        subtitle = QLabel("手写账簿")
        subtitle.setObjectName("brandSub")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand.addLayout(brand_text)
        layout.addLayout(brand)

        layout.addSpacing(20)
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        rule.setContentsMargins(0, 0, 0, 0)
        rule_wrap = QHBoxLayout()
        rule_wrap.setContentsMargins(22, 0, 22, 0)
        rule_wrap.addWidget(rule)
        layout.addLayout(rule_wrap)
        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        current_section = None
        for index, (nav_label, _title, _sub, section_name) in enumerate(NAV_ITEMS):
            if section_name != current_section:
                if current_section is not None:
                    layout.addSpacing(10)
                section = QLabel(section_name)
                section.setObjectName("sectionLabel")
                section_wrap = QHBoxLayout()
                section_wrap.setContentsMargins(22, 0, 22, 6)
                section_wrap.addWidget(section)
                layout.addLayout(section_wrap)
                current_section = section_name

            button = QPushButton(nav_label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, idx=index: self._switch_page(idx))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
            self._nav_buttons.append(button)

        layout.addStretch(1)

        bottom = QVBoxLayout()
        bottom.setContentsMargins(18, 0, 18, 0)
        bottom.setSpacing(7)
        account_label = QLabel("当前账户")
        account_label.setObjectName("sectionLabel")
        bottom.addWidget(account_label)

        self.account_combo = PaperComboBox()
        self.account_combo.setMinimumHeight(32)
        self.account_combo.currentIndexChanged.connect(self._on_account_changed)
        bottom.addWidget(self.account_combo)

        manage_hint = QLabel("账户的增删在「账户设置」页")
        manage_hint.setObjectName("sidebarFoot")
        manage_hint.setWordWrap(True)
        bottom.addWidget(manage_hint)

        foot = QLabel("本地 SQLite · 离线运行 · 无云端依赖")
        foot.setObjectName("sidebarFoot")
        foot.setWordWrap(True)
        bottom.addWidget(foot)

        version = QLabel(f"V{APP_VERSION}")
        version.setObjectName("sidebarVersion")
        bottom.addWidget(version)
        layout.addLayout(bottom)

        return side

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(3)
        self.page_title = QLabel(NAV_ITEMS[HOLDINGS_PAGE][1])
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel("")
        self.page_subtitle.setObjectName("pageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        row.addLayout(titles)
        row.addStretch(1)

        self.quote_sync_button = QPushButton("行情同步")
        self.quote_sync_button.setObjectName("ghostButton")
        self.quote_sync_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.quote_sync_button.setToolTip(
            "补全当前持仓标的从上次行情日到今天的日线，并同步分红送转"
        )
        self.quote_sync_button.clicked.connect(self._sync_quotes)
        row.addWidget(self.quote_sync_button)

        refresh = QPushButton("刷新")
        refresh.setObjectName("ghostButton")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self._reload_all)
        row.addWidget(refresh)
        return row

    def _build_overview(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        self.card_total = MetricCard("总资产")
        self.card_cash = MetricCard("可用现金")
        self.card_market = MetricCard("持仓市值")
        self.card_pnl = MetricCard("持仓盈亏")
        for card in (self.card_total, self.card_cash, self.card_market, self.card_pnl):
            row.addWidget(card, 1)
        return row

    def _table_card(
        self, title: str, hint: str = ""
    ) -> tuple[QFrame, PaperTableView, QLabel]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        hint_label = QLabel(hint)
        hint_label.setObjectName("cardHint")
        count_label = QLabel("")
        count_label.setObjectName("cardHint")
        head.addWidget(title_label)
        head.addWidget(hint_label)
        head.addStretch(1)
        head.addWidget(count_label)

        view = PaperTableView()
        self._table_views.append(view)
        layout.addLayout(head)
        layout.addWidget(view, 1)
        return card, view, count_label

    def _build_holdings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, self.holdings_view, self.holdings_count = self._table_card(
            "持仓汇总", "按证券代码汇总 · 原始成本锁定，摊薄成本按券商口径 · 右键看标的明细"
        )
        self.holdings_model = HoldingsModel()
        self._prepare_table(self.holdings_view, self.holdings_model)
        self.holdings_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.holdings_view.customContextMenuRequested.connect(self._on_holdings_menu)

        self.holdings_empty = QLabel("暂无持仓 · 可在左侧「买卖」页记一笔成交")
        self.holdings_empty.setObjectName("emptyHint")
        self.holdings_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        note = QLabel(
            "原始成本为剩余批次的买入成本（含费）；摊薄成本 =（累计买入含费 − 累计卖出净额 − 累计税前分红）÷ 当前股数，"
            "与券商成本价一致（整仓清空后重新开仓从新一轮起算）；盈亏 = 市值 − 摊薄成本 × 股数，含已实现盈亏。"
            "缺失行情时按成本估值。"
        )
        note.setObjectName("cardHint")
        note.setWordWrap(True)

        body = card.layout()
        body.addWidget(self.holdings_empty)
        body.addWidget(note)

        layout.addWidget(card, 1)
        return page

    def _build_trade_page(self) -> QWidget:
        """买卖合一的录入页：一张表单 + 横排双按钮 + 全部成交（右键删改）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.setContentsMargins(22, 18, 22, 18)
        form.setSpacing(14)

        head = QHBoxLayout()
        title_label = QLabel("记一笔成交")
        title_label.setObjectName("cardTitle")
        hint_label = QLabel("卖出按加权摊薄成本结转盈亏，批次仍按 FIFO 补扣红利税")
        hint_label.setObjectName("cardHint")
        head.addWidget(title_label)
        head.addStretch(1)
        head.addWidget(hint_label)
        form.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)

        symbol = HistoryLineEdit(self.symbol_history)
        symbol.setPlaceholderText("如 600000 / 510300（点击可看历史代码）")
        symbol.setMaxLength(12)
        symbol.setClearButtonEnabled(True)

        trade_date = YearTailDateEdit(QDate.currentDate())
        trade_date.setDisplayFormat("yyyy-MM-dd")
        trade_date.setCalendarPopup(True)

        qty = QSpinBox()
        qty.setRange(1, 100_000_000)
        qty.setSingleStep(100)
        qty.setValue(100)
        qty.setGroupSeparatorShown(True)

        price = QDoubleSpinBox()
        price.setDecimals(3)
        price.setRange(0.001, 100_000.0)
        price.setSingleStep(0.01)
        price.setValue(10.0)

        specs = ((("证券代码", "symbol"), symbol), (("成交日期", "date"), trade_date),
                 (("数量 (股)", "qty"), qty), (("成交价 (元)", "price"), price))
        inputs: dict[str, QWidget] = {}
        for column, ((label_text, key), widget) in enumerate(specs):
            label = QLabel(label_text)
            label.setObjectName("fieldLabel")
            grid.addWidget(label, 0, column)
            grid.addWidget(widget, 1, column)
            grid.setColumnStretch(column, 1)
            inputs[key] = widget
        form.addLayout(grid)

        # 行情行：按代码+日期拉取当日 OHLC；「开/高/低/收」四个小按钮把对应
        # 价格点选进「成交价」输入框，免手抄
        quote_row = QHBoxLayout()
        quote_row.setSpacing(8)
        fetch = QPushButton("取当日行情")
        fetch.setObjectName("ghostButton")
        fetch.setCursor(Qt.CursorShape.PointingHandCursor)
        fetch.clicked.connect(self._query_trade_quote)
        quote_row.addWidget(fetch)

        self.quote_label = QLabel("输入代码与日期后，一键取价（自动填入收盘价，可改）")
        self.quote_label.setObjectName("cardHint")
        quote_row.addWidget(self.quote_label, 1)

        pick_hint = QLabel("点选填入成交价：")
        pick_hint.setObjectName("cardHint")
        quote_row.addWidget(pick_hint)

        inputs["fetch"] = fetch
        for tag, text in (("open", "开"), ("high", "高"), ("low", "低"), ("close", "收")):
            pick = QPushButton(text)
            pick.setObjectName("ghostButton")
            pick.setCursor(Qt.CursorShape.PointingHandCursor)
            pick.setEnabled(False)
            pick.setToolTip("点击将该价格填入成交价")
            pick.clicked.connect(lambda _=False, t=tag: self._fill_quote_price(t))
            quote_row.addWidget(pick)
            inputs[f"pick_{tag}"] = pick
        form.addLayout(quote_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(14)
        buy = QPushButton("确认买入")
        sell = QPushButton("确认卖出")
        for button, side in ((buy, SIDE_BUY), (sell, SIDE_SELL)):
            button.setObjectName("primaryButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, s=side: self._submit_trade(s))
        status = QLabel("")
        status.setObjectName("formStatus")
        status.setWordWrap(True)
        action_row.addWidget(buy)
        action_row.addWidget(sell)
        action_row.addSpacing(10)
        action_row.addWidget(status, 1)
        form.addLayout(action_row)
        inputs["buy"] = buy
        inputs["sell"] = sell

        layout.addWidget(card)

        trades_card, self.trades_view, self.trades_count = self._table_card(
            "成交记录", "全部成交 · 右键可修改 / 删除，删改后全账自动重算"
        )
        self.trades_model = RecordsModel(
            ("日期", "方向", "证券代码", "证券名称", "数量", "成交价",
             "佣金", "印花税", "过户费", "净额", "已实现盈亏"),
            right_aligned={4, 5, 6, 7, 8, 9, 10},
        )
        self._prepare_table(self.trades_view, self.trades_model)
        # 默认按日期倒序：最近的成交在最上面（与数据装载顺序一致）
        self.trades_view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.trades_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.trades_view.customContextMenuRequested.connect(self._on_trades_menu)
        layout.addWidget(trades_card, 1)

        self.trade_inputs = inputs
        self.trade_status = status
        return page

    def _prepare_table(self, view: PaperTableView, model) -> None:
        view.setModel(model)
        # 表头点击排序：QTableView 会调用 model.sort(column, order)
        view.setSortingEnabled(True)
        view.setAlternatingRowColors(True)
        view.setShowGrid(False)
        view.verticalHeader().setVisible(False)
        view.horizontalHeader().setHighlightSections(False)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setWordWrap(False)
        view.setCornerButtonEnabled(False)
        self._apply_table_font(view)

    def _apply_table_font(self, view: PaperTableView) -> None:
        px = TABLE_FONT_PX
        header_px = max(11, px - 2)
        view.setStyleSheet(
            f"QTableView {{ font-size: {px}px; }}"
            f"QHeaderView::section {{ font-size: {header_px}px; }}"
        )
        view.verticalHeader().setDefaultSectionSize(px + 20)

    # ------------------------------------------------------------ 交互
    def _switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self._nav_buttons):
            button.setChecked(i == index)
        if index == INGEST_PAGE:
            # 下载结果是本地行情概览，每次进入都按 quotes 表重新回填
            self._refresh_download_results()
        self._refresh_header()

    def _refresh_header(self) -> None:
        index = self.stack.currentIndex()
        _nav, title, subtitle, _section = NAV_ITEMS[index]
        if index == HOLDINGS_PAGE:
            subtitle = f"{date.today().isoformat()} · 按最新收盘价估值"
        if self._migration_note:
            subtitle = f"{subtitle} · {self._migration_note}" if subtitle else self._migration_note
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)

    def symbol_history(self) -> list[str]:
        """历史输入过的证券代码：最近成交在前，其次公司行为。"""
        codes: dict[str, None] = {}
        for trade in reversed(self.service.portfolio.trades):
            codes.setdefault(trade.symbol, None)
        for row in self.service.repo.list_corporate_actions():
            codes.setdefault(row["symbol"], None)
        return list(codes)

    def _on_account_changed(self, index: int) -> None:
        account_id = self.account_combo.itemData(index)
        if account_id is None:
            return
        account_id = int(account_id)
        if account_id == self.account_id:
            return
        self.account_id = account_id
        self._reload_all()
        # 换账户后数据宽度会变：重排一次列宽，隐藏页等切过去时再排
        for view in self._table_views:
            view.reset_column_widths()

    def _query_trade_quote(self) -> None:
        """按代码+日期拉当日不复权 OHLC，收盘价自动填入成交价。"""
        symbol = self.trade_inputs["symbol"].text().strip()
        if not symbol:
            self._set_trade_status("请先填写证券代码", ok=False)
            return
        qdate = self.trade_inputs["date"].date()
        day = date(qdate.year(), qdate.month(), qdate.day())

        self.quote_label.setText("正在获取行情 …")

        def work():
            return get_daily_ohlc(symbol, day, timeout=8)

        self._run_task(
            work,
            self._on_trade_quote,
            lambda msg: self.quote_label.setText(f"行情获取失败：{msg}"),
            self.trade_inputs["fetch"],
        )

    def _on_trade_quote(self, quote) -> None:
        if not quote.ok:
            self.quote_data = {}
            for tag in ("open", "high", "low", "close"):
                self.trade_inputs[f"pick_{tag}"].setEnabled(False)
            self.quote_label.setText(f"{quote.ticker} 未取到行情：{quote.error}")
            return

        actual = quote.price_date.isoformat()
        note = "" if quote.is_exact_date else "（该日无成交，已回退到最近交易日）"
        fmt = lambda v: "—" if v is None else f"{float(v):.3f}"
        self.quote_label.setText(
            f"{quote.ticker} {self.name_of(quote.ticker)} · {actual}{note}　"
            f"前收 {fmt(quote.prev_close)}　开 {fmt(quote.open)}　"
            f"高 {fmt(quote.high)}　低 {fmt(quote.low)}　收 {fmt(quote.close)}"
        )
        self.quote_data = {
            "open": quote.open,
            "high": quote.high,
            "low": quote.low,
            "close": quote.close,
        }
        for tag in ("open", "high", "low", "close"):
            self.trade_inputs[f"pick_{tag}"].setEnabled(self.quote_data[tag] is not None)
        if quote.close is not None:  # 默认填收盘价，用户仍可手改或点其他价位
            self.trade_inputs["price"].setValue(float(quote.close))

    def _fill_quote_price(self, tag: str) -> None:
        price = self.quote_data.get(tag)
        if price is not None:
            self.trade_inputs["price"].setValue(float(price))

    def _set_trade_buttons(self, enabled: bool) -> None:
        enabled = enabled and not self._statement_mode
        self.trade_inputs["buy"].setEnabled(enabled)
        self.trade_inputs["sell"].setEnabled(enabled)

    def _submit_trade(self, side: str) -> None:
        if self._statement_mode:
            self._set_trade_status("交割单口径不支持手动记账，请导入交割单。", ok=False)
            return
        if self._ledger_busy:
            return
        inputs = self.trade_inputs
        symbol = inputs["symbol"].text().strip()
        if not symbol:
            self._set_trade_status("请先填写证券代码", ok=False)
            return
        qdate = inputs["date"].date()
        trade_date = date(qdate.year(), qdate.month(), qdate.day())
        qty = inputs["qty"].value()
        price = inputs["price"].value()

        # 成交前的可选复盘：留空直接入账，填写后进入明细查看
        plan = TradePlanDialog(side, symbol, self)
        if plan.exec() != QDialog.DialogCode.Accepted:
            return
        notes = plan.values()

        label = "买入" if side == SIDE_BUY else "卖出"
        self._set_trade_status(f"正在同步 {symbol} 分红送转并提交{label} …")
        self._ledger_busy = True
        self._set_trade_buttons(False)
        conn, account_id = self.conn, self.account_id

        def work():
            # 先自动落账该标的历史公司行为（按在册批次过滤，不会错补）；
            # 同步失败只备注、不阻断成交，避免网络问题卡住记账。
            note = ""
            try:
                sync = CorporateActionService(conn, account_id).fetch_and_sync_dividends(
                    symbol, timeout=8
                )
                if sync.applied:
                    note = f"；同步落账 {sync.applied} 条分红送转"
                elif sync.errors:
                    note = f"；公司行为同步未全成功：{'；'.join(sync.errors[:2])}"
            except Exception as exc:
                note = f"；公司行为同步失败（不影响成交）：{exc}"

            service = LedgerService(conn, account_id)
            if side == SIDE_BUY:
                trade = service.buy(symbol, trade_date, qty, str(price), notes=notes)
            else:
                trade = service.sell(symbol, trade_date, qty, str(price), notes=notes)
            return trade, note

        def done(result) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            self._on_trade_done(side, result)

        def error(msg: str) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            self._set_trade_status(f"未成交：{msg}", ok=False)

        self._run_task(work, done, error)

    def _on_trade_done(self, side: str, result) -> None:
        trade, note = result
        label = "买入" if side == SIDE_BUY else "卖出"
        fee = trade.fee_total_cents / 100
        self._set_trade_status(
            f"已{label} {trade.qty:,} 股 @ {trade.price_mills / 1000:.3f}，"
            f"手续费 {fee:,.2f} 元，已写入账簿{note}。",
            ok=True,
        )
        self._reload_all()

    def _set_trade_status(self, text: str, ok: bool = True) -> None:
        color = COLORS["accent"] if ok else COLORS["up"]
        self.trade_status.setStyleSheet(f"color: {color}; background: transparent;")
        self.trade_status.setText(text)

    # ------------------------------------------------------------ 成交删改
    def _on_trades_menu(self, pos) -> None:
        index = self.trades_view.indexAt(pos)
        if not index.isValid():
            return
        trade = self.trades_model.rows()[index.row()].payload
        if trade is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction("修改…")
        delete_action = menu.addAction("删除")
        chosen = menu.exec(self.trades_view.viewport().mapToGlobal(pos))
        if chosen is edit_action:
            self.edit_trade(trade)
        elif chosen is delete_action:
            self.delete_trade(trade)

    def edit_trade(self, trade) -> None:
        """修改一笔成交：保存后按剩余流水全量重算。"""
        if self._statement_mode:
            warn(self, "交割单口径", "该账户只认导入的交割单，不能手动修改成交。")
            return
        if self._ledger_busy:
            return
        dialog = TradeEditDialog(trade, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        symbol, trade_date, qty, price = dialog.values()
        notes = dialog.notes()
        self._ledger_busy = True
        self._set_trade_buttons(False)
        conn, account_id = self.conn, self.account_id

        def work():
            LedgerService(conn, account_id).update_trade(
                trade.trade_id, symbol=symbol, trade_date=trade_date,
                qty=qty, price_yuan=price, notes=notes,
            )
            return symbol, trade_date, qty, price

        def done(result) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            symbol, trade_date, qty, price = result
            self._set_trade_status(
                f"已修改成交：{trade_date.isoformat()} {symbol} {qty:,} 股 @ {price} 元，"
                "账簿已按剩余流水全量重算。"
            )
            self._reload_all()

        def error(msg: str) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            self._set_trade_status(f"修改未生效，账簿保持原样：{msg}", ok=False)

        self._run_task(work, done, error)

    def delete_trade(self, trade) -> None:
        """物理删除一笔成交：确认后按剩余流水全量重算。"""
        if self._statement_mode:
            warn(self, "交割单口径", "该账户只认导入的交割单，不能手动删除成交。")
            return
        if self._ledger_busy:
            return
        side = "卖出" if trade.side == SIDE_SELL else "买入"
        if not confirm(
            self,
            "删除成交",
            f"确定删除 {trade.trade_date.isoformat()} {trade.symbol} {side} "
            f"{trade.qty:,} 股 @ {trade.price_mills / 1000:.3f} 元这笔成交吗？\n"
            "删除后将按剩余流水重新计算批次、现金、红利税与公司行为。",
            ok_text="删除",
        ):
            return
        self._ledger_busy = True
        self._set_trade_buttons(False)
        conn, account_id = self.conn, self.account_id

        def work():
            LedgerService(conn, account_id).delete_trades([trade.trade_id])

        def done(_result) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            self._set_trade_status(
                f"已删除 {trade.trade_date.isoformat()} {trade.symbol} 那笔{side}，"
                "账簿已按剩余流水全量重算。"
            )
            self._reload_all()

        def error(msg: str) -> None:
            self._ledger_busy = False
            self._set_trade_buttons(True)
            self._set_trade_status(f"删除未生效，账簿保持原样：{msg}", ok=False)

        self._run_task(work, done, error)

    # ------------------------------------------------------------ 标的明细
    def _on_holdings_menu(self, pos) -> None:
        index = self.holdings_view.indexAt(pos)
        if not index.isValid():
            return
        row = self.holdings_model.rows()[index.row()]
        menu = QMenu(self)
        detail_action = menu.addAction("明细")
        chosen = menu.exec(self.holdings_view.viewport().mapToGlobal(pos))
        if chosen is detail_action:
            self._open_symbol_detail(row.symbol)

    def _open_symbol_detail(self, symbol: str) -> None:
        # 同标的已开着就直接提到前台，避免重复窗口
        for dialog in self._detail_dialogs:
            if dialog.symbol == symbol:
                dialog.raise_()
                dialog.activateWindow()
                return
        dialog = SymbolDetailDialog(self, symbol)
        self._detail_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _=0, d=dialog: self._detail_dialogs.remove(d)
            if d in self._detail_dialogs else None
        )
        dialog.show()

    # ------------------------------------------------------------ 账户增删
    def _refresh_accounts(self, select_id: int | None = None) -> None:
        self.account_rows = AccountService(self.conn).list_accounts_with_summary()
        ids = [int(row["account_id"]) for row in self.account_rows]
        if not ids:
            return
        if select_id not in ids:
            select_id = ids[0]
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for row in self.account_rows:
            self.account_combo.addItem(row["name"], int(row["account_id"]))
        self.account_combo.setCurrentIndex(ids.index(select_id))
        self.account_combo.blockSignals(False)
        self.account_id = select_id

    def _add_account(self) -> None:
        dialog = NewAccountDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, initial_cents, ledger_mode = dialog.values()
        try:
            new_id = AccountService(self.conn).create(name, initial_cents, ledger_mode)
        except ValueError as exc:
            warn(self, "新建账户", str(exc))
            return
        self._refresh_accounts(new_id)
        self._reload_all()

    def _remove_account(self) -> None:
        name = self.account_combo.currentText()
        if not confirm(
            self,
            "删除账户",
            f"确定删除账户「{name}」吗？\n"
            "该账户的成交、批次、公司行为与现金流水将一并删除，且不可恢复。",
            ok_text="删除",
        ):
            return
        try:
            AccountService(self.conn).delete(self.account_id)
        except ValueError as exc:
            warn(self, "删除账户", str(exc))
            return
        self._refresh_accounts()
        self._reload_all()

    # ------------------------------------------------------------ 证券名称
    def name_of(self, symbol: str) -> str:
        """静态速查表优先，其次 meta 缓存；都没有显示破折号。"""
        static = display_name(symbol)
        return static if static != "—" else self.names.get(symbol, "—")

    def _ensure_name_cache(self, pf) -> None:
        """收集当前账簿出现过的代码，缺名称的后台补全（每次会话只请求一次）。"""
        symbols = {trade.symbol for trade in pf.trades}
        symbols |= {symbol for symbol, book in pf.books.items() if book.total_qty > 0}
        symbols |= {row["symbol"] for row in self.service.repo.list_corporate_actions()}
        missing = [
            s for s in symbols
            if self.name_of(s) == "—" and s not in self._names_requested
        ]
        if not missing:
            return
        self._names_requested.update(missing)
        conn = self.conn

        def work():
            return ensure_names(conn, missing, timeout=8)

        self._run_task(work, self._on_names_ready, lambda _msg: None)

    def _on_names_ready(self, resolved: dict) -> None:
        if resolved:
            self.names.update(resolved)
            self._reload_all()

    # ------------------------------------------------------------ 行情同步
    def _sync_quotes(self) -> None:
        """补全当前持仓标的的日线，并同步分红送转。

        起点：该标的已下载的最后一根日线；从未下载过则取本账户首笔成交日。
        交割单口径的红利与送转只认交割单，这里只补行情，不同步公司行为。
        """
        today = date.today()
        symbols = sorted(
            symbol
            for symbol, book in self.service.portfolio.books.items()
            if book.total_qty > 0
        )
        if not symbols:
            warn(self, "行情同步", "当前账户没有持仓标的，无需同步行情。")
            return

        conn, account_id = self.conn, self.account_id
        sync_actions = not self.service.is_statement_mode()
        self.page_subtitle.setText(f"正在同步 {len(symbols)} 个持仓标的的行情 …")

        def work():
            repo = Repository(conn, account_id)
            quote_rows = []
            for symbol in symbols:
                last_day = repo.latest_quote_date(symbol)
                # 有历史行情就从最后一天起重下（覆盖同值，避免非交易日被判「无数据」）；
                # 从未下载过则从本账户首笔成交日补起。
                start = last_day or repo.first_trade_date(symbol) or (
                    today - timedelta(days=365 * 3)
                )
                try:
                    result = download_history(repo, [symbol], start, today)[0]
                except Exception as exc:
                    quote_rows.append((symbol, 0, start, today, str(exc)))
                    continue
                quote_rows.append(
                    (
                        symbol,
                        result.rows,
                        result.first_date or start,
                        result.last_date or start,
                        result.error or "",
                    )
                )

            action_rows = []
            if sync_actions:
                service = CorporateActionService(conn, account_id)
                for symbol in symbols:
                    try:
                        result = service.fetch_and_sync_dividends(symbol, until=today)
                    except Exception as exc:
                        action_rows.append((symbol, 0, "", str(exc)))
                        continue
                    action_rows.append(
                        (
                            symbol,
                            result.applied,
                            result.note or "；".join(result.errors),
                            "",
                        )
                    )

            missing = [symbol for symbol in symbols if self.name_of(symbol) == "—"]
            resolved = ensure_names(conn, missing, timeout=8) if missing else {}
            return quote_rows, action_rows, resolved, sync_actions

        self._run_task(
            work,
            self._on_quote_sync_done,
            self._on_quote_sync_failed,
            self.quote_sync_button,
        )

    def _on_quote_sync_failed(self, message: str) -> None:
        self._reload_all()
        warn(self, "行情同步失败", message)

    def _on_quote_sync_done(self, payload) -> None:
        quote_rows, action_rows, resolved, sync_actions = payload
        if resolved:
            self.names.update(resolved)

        failed = [row for row in quote_rows if row[4]]
        written = [row for row in quote_rows if not row[4] and row[1]]
        lines = [
            f"行情：{len(written)} 个标的已更新，{len(failed)} 个失败"
            f"（共 {len(quote_rows)} 个持仓标的）。"
        ]
        for symbol, _rows, first_day, last_day, _error in written[:3]:
            lines.append(f"· {symbol} {first_day} ~ {last_day}")
        for symbol, _rows, _start, _end, error in failed[:5]:
            lines.append(f"· {symbol} 失败：{error}")

        if sync_actions:
            applied = sum(row[1] for row in action_rows)
            warnings = [row for row in action_rows if row[2]]
            lines.append(f"公司行为：新增 {applied} 条事件，{len(warnings)} 个标的有提示。")
            for symbol, _applied, note, _error in warnings[:3]:
                lines.append(f"· {symbol}：{note}")
        else:
            lines.append("公司行为：交割单口径以交割单流水为准，本次已跳过。")

        self._reload_all()
        warn(self, "行情同步完成", "\n".join(lines))

    def _apply_ledger_mode_ui(self, statement: bool) -> None:
        """交割单口径：只认导入的交割单，手动记账入口全部停用。"""
        self._set_trade_buttons(not self._ledger_busy)
        for button in (self.deposit_button, self.withdraw_button, self.dividend_button):
            button.setEnabled(not statement)
        if statement:
            self._set_trade_status(
                "当前账户是「交割单口径」：费用、红利与现金全部以导入的交割单为准，"
                "「记一笔」与手动出入金已停用。",
                ok=False,
            )
            if self.capital_status.text().startswith("已有成交时"):
                self.capital_status.setText("期初现金会直接计入可用现金；费率与红利以交割单为准。")
        else:
            if self.trade_status.text().startswith("当前账户是「交割单口径」"):
                self.trade_status.setText("")
            if self.capital_status.text().startswith("期初现金"):
                self.capital_status.setText("已有成交时，改初始本金只调整净值基准，不动现金")

    # ------------------------------------------------------------ 数据装载
    def _reload_all(self) -> None:
        try:
            self.service = LedgerService(self.conn, self.account_id)
        except Exception as exc:
            self.page_subtitle.setText(f"读取账户失败：{exc}")
            return

        pf = self.service.portfolio
        today = date.today()
        prices: dict[str, int] = {}
        for symbol, book in pf.books.items():
            if book.total_qty <= 0:
                continue
            price = self.service.repo.latest_price(symbol, today)
            if price is not None:
                prices[symbol] = price

        summary = self.service.holdings_summary(today)
        holdings = [
            HoldingRow(
                symbol=row["代码"],
                name=self.name_of(row["代码"]),
                qty=row["持仓股数"],
                original_cost=row["原始成本(元/股)"],
                diluted_cost=row["摊薄成本(元/股)"],
                dividend=row["累计现金分红(元)"],
                price=row["最新价(元)"],
                market_value=row["市值(元)"],
                pnl=row["持仓盈亏(元)"],
                pnl_ratio=row["盈亏比例(%)"],
            )
            for row in summary
        ]
        self.holdings_model.set_rows(holdings)
        self.holdings_view.setVisible(bool(holdings))
        self.holdings_empty.setVisible(not holdings)
        self.holdings_count.setText(f"{len(holdings)} 个标的" if holdings else "空账簿")

        self._update_overview(pf, prices, holdings, summary)
        self._update_trades(pf)
        self._statement_mode = self.service.is_statement_mode()
        self._refresh_account_fields()
        self._refresh_header()
        self._apply_ledger_mode_ui(self._statement_mode)
        self._refresh_statement_pick_button()
        self._ensure_name_cache(pf)
        for dialog in list(self._detail_dialogs):
            dialog.refresh_rows()

    def _update_overview(self, pf, prices, holdings, summary) -> None:
        total = pf.total_asset_cents(prices) / 100
        cash = pf.cash_cents / 100
        market = pf.market_value_cents(prices) / 100
        initial = self.service.initial_capital_cents() / 100
        base = self.service.capital_base_cents() / 100

        pnl_total = sum(row.pnl for row in holdings if row.pnl is not None)
        # 券商的盈亏率分母是摊薄成本：市值 − 持仓盈亏 即为摊薄总成本
        cost_with_price = sum(
            row["市值(元)"] - row["持仓盈亏(元)"]
            for row in summary
            if row["市值(元)"] is not None and row["持仓盈亏(元)"] is not None
        )
        dividend_total = sum(row["累计现金分红(元)"] for row in summary)
        missing = sum(1 for row in summary if row["最新价(元)"] is None)

        if base > 0:
            nav = total / base
            label = "初始本金" if initial > 0 else "投入本金"
            self.card_total.set_hint(f"{label} {_money(base)} · 净值 {nav:.4f}")
        else:
            self.card_total.set_hint("尚未设置初始本金")
        self.card_total.set_value(_money(total))

        self.card_cash.set_value(_money(cash))
        self.card_cash.set_hint(
            f"现金占比 {cash / total * 100:.1f}%" if total > 0 else "尚未入金"
        )

        self.card_market.set_value(_money(market))
        market_hint = f"{len(summary)} 个标的"
        if missing:
            market_hint += f" · {missing} 个按成本估值"
        self.card_market.set_hint(market_hint)

        tone = "up" if pnl_total > 0 else "down" if pnl_total < 0 else None
        self.card_pnl.set_value(_money(pnl_total), tone)
        if cost_with_price > 0:
            ratio = pnl_total / cost_with_price * 100
            self.card_pnl.set_hint(
                f"摊薄成本 {_money(cost_with_price)} · 累计分红 {_money(dividend_total)}"
                f" · {ratio:+.2f}%（含已实现盈亏）"
            )
        else:
            self.card_pnl.set_hint("暂无持仓成本可比较")

    def _update_trades(self, pf) -> None:
        """全部成交（最新在前），每行 payload 挂原始成交供右键删改。"""
        rows = [
            trade_record_row(trade, self.name_of(trade.symbol))
            for trade in reversed(pf.trades)
        ]
        self.trades_model.set_rows(rows)
        self.trades_count.setText(f"{len(rows)} 笔" if rows else "暂无记录")

    # ------------------------------------------------------------ 状态
    def _restore_geometry(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 等待后台下载/同步线程收尾，避免线程仍在写库时窗口被销毁
        for task in list(self._tasks):
            task.wait(3000)
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
