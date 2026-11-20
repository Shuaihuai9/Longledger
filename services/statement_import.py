"""券商交割单智能解析：嗅探列名、定位表头、过滤非交易记录、统一金额口径。

本模块只负责「文件 -> 标准成交」，不写数据库。落账由
`LedgerService.import_trades` 在一次全量重放中完成，批次、现金与红利税
全部由既有账务引擎重算。

设计要点：
- 列名不硬编码，按关键词模糊匹配并带权重打分（「证券名称」不会被当成 ticker）；
- 自动丢弃流水号、委托号、资金余额等系统不需要的列；
- 银行转证券、申购赎回等非买卖记录直接隔离，绝不写进账本；
- 红利入账、股息红利差异、利息归本等真实现金流水按「发生金额」补入现金，
  否则只重放成交时资金会中途见底（买成价之外还有分红到账、利息结息）。
  银行转账视为已体现在账户初始本金、逆回购视为资金短暂停放，两者都不入库；
- CSV 兼容 UTF-8 / UTF-8-SIG / GBK / GB18030，Excel 兼容 .xlsx / .xlsm / .xls；
- 表头前有标题、账户信息等废话行时，扫描前若干行按匹配得分自动定位；
- 金额统一转「分」、价格转「毫」，日期统一为 `datetime.date`。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.money import qty_price_to_cents, round_half_up

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

# 表头最多向下扫描行数：兼容银行/券商在文件头部加标题、账户、导出时间等废话行
_HEADER_SCAN_LIMIT = 40

# 真 .xls（OLE 复合文档）的文件头；其余以 .xls 结尾的多半是券商导出的文本
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# 字段 -> (关键词, 权重)。权重越高越优先；同一列命中多个关键词取最高权重。
_FIELD_RULES: dict[str, tuple[tuple[str, int], ...]] = {
    "ticker": (
        ("证券代码", 60),
        ("股票代码", 60),
        ("证券编码", 58),
        ("证券代号", 58),
        ("股票代号", 58),
        ("基金代码", 55),
        ("代码", 40),
        ("证券", 30),
        ("ticker", 50),
        ("symbol", 40),
        ("code", 30),
    ),
    "name": (
        ("证券名称", 60),
        ("股票名称", 60),
        ("证券简称", 55),
        ("名称", 45),
        ("简称", 40),
        ("name", 40),
    ),
    "date": (
        ("成交日期", 60),
        ("发生日期", 60),
        ("交易日期", 55),
        ("成交时间", 45),
        ("发生时间", 45),
        ("交易日", 45),
        ("日期", 40),
        ("date", 40),
    ),
    "action": (
        ("买卖标志", 60),
        ("买卖方向", 60),
        ("买卖类型", 58),
        ("交易类型", 55),
        ("业务类型", 52),
        ("交易类别", 50),
        ("买卖", 50),
        ("操作", 45),
        ("业务名称", 45),
        ("交易名称", 42),
        ("方向", 40),
        ("摘要", 30),
        ("side", 40),
        ("action", 40),
    ),
    "qty": (
        ("成交数量", 60),
        ("成交股数", 60),
        ("成交数", 58),
        ("发生数量", 55),
        ("成交量", 50),
        ("数量", 45),
        ("股数", 45),
        ("qty", 45),
        ("quantity", 45),
        ("volume", 30),
    ),
    "price": (
        ("成交均价", 60),
        ("成交价格", 60),
        ("成交单价", 58),
        ("成交价", 52),
        ("每股价格", 50),
        ("均价", 45),
        ("价格", 40),
        ("price", 40),
    ),
    "commission": (
        ("券商佣金", 62),
        ("手续费", 60),
        ("佣金", 60),
        ("交易费用", 50),
        ("费用", 40),
        ("commission", 50),
        ("fee", 40),
    ),
    "amount": (
        ("发生金额", 75),
        ("发生额", 65),
        ("收付金额", 62),
        ("收付额", 55),
        ("净额", 50),
        ("amount", 45),
    ),
    "stamp": (
        ("印花税", 80),
        ("stamp", 60),
    ),
    "transfer": (
        ("过户费", 80),
        ("transfer", 60),
    ),
    "other_fee": (
        ("其他杂费", 75),
        ("其他费用", 65),
        ("杂费", 55),
        ("规费", 50),
        ("证管费", 45),
        ("经手费", 45),
    ),
    "balance": (
        ("资金余额", 80),
        ("资金账户余额", 75),
        ("可用余额", 70),
        ("可用资金", 68),
        ("余额", 60),
        ("balance", 50),
    ),
    "share_balance": (
        ("股票余额", 85),
        ("股份余额", 80),
        ("证券余额", 78),
        ("持股余额", 75),
        ("持仓数量", 60),
        ("当前持股", 55),
    ),
}

# 负向关键词：同名不同义的列（委托数量 / 委托价格 / 市值 / 余额）要降权。
# 只降权不直接排除：某些券商确实只有「委托数量」，命中后仍可兜底识别。
_FIELD_NEGATIVES: dict[str, tuple[tuple[str, int], ...]] = {
    "ticker": (("名称", 80), ("简称", 80), ("笔数", 40)),
    "qty": (("委托", 25), ("笔数", 50), ("市值", 40), ("余额", 40), ("可用", 40)),
    "price": (("委托", 25), ("涨跌", 40), ("幅度", 40), ("市值", 40)),
    "date": (("交收", 15), ("清算", 10)),
    "amount": (("余额", 50), ("市值", 50), ("手续费", 30)),
    "balance": (("股票", 70), ("股份", 70), ("持仓", 70), ("持股", 70)),
}

# 同分时的字段优先顺序，避免「证券名称」抢走 ticker 之类的边界情况
_FIELD_ORDER = (
    "ticker",
    "date",
    "action",
    "qty",
    "price",
    "commission",
    "stamp",
    "transfer",
    "other_fee",
    "amount",
    "name",
    "balance",
    "share_balance",
)

# 必须识别出的列；缺失时给出面向用户的明确提示
_REQUIRED_MESSAGES: tuple[tuple[str, str], ...] = (
    ("ticker", "未能识别出证券代码列，请检查文件格式"),
    ("date", "未能识别出成交日期列，请检查文件格式"),
    ("action", "未能识别出买卖方向列，请检查文件格式"),
    ("qty", "未能识别出成交数量列，请检查文件格式"),
    ("price", "未能识别出成交价格列，请检查文件格式"),
)

# 非买卖交易标记：命中即隔离，不写入账本
_NON_TRADE_MARKERS = (
    "银行转证券",
    "证券转银行",
    "银证转账",
    "银行转账",
    "银行转",
    "转证券",
    "划转",
    "转托管",
    "转入",
    "转出",
    "红利入账",
    "红利",
    "红股",
    "股息",
    "分红",
    "申购",
    "赎回",
    "认购",
    "中签",
    "配股",
    "质押",
    "解押",
    "冻结",
    "解冻",
    "基金转换",
    "新股",
    "送股",
    "转增",
    "拆股",
    "合股",
    "撤单",
    "废单",
    # 券商对账单里的现金管理与账户操作，同样不是买卖成交
    "逆回购",
    "通用回购",
    "国债回购",
    "利息归本",
    "指定交易",
    "指定登记",
    "dividend",
    "transfer",
    "subscribe",
    "redemption",
)

_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5")
_DELIMITERS = (",", "\t", ";", "|")

# 银行入金/出金：账户初始本金已代表累计入金，再按交割单入账会重复计入
_TRANSFER_MARKERS = ("银行转证券", "证券转银行", "银证转账", "银行转账", "银行转", "转证券")

# 逆回购只是资金短暂停放，本系统不为回购单独建仓，本金仍算在现金里
_REPO_MARKERS = ("逆回购", "通用回购", "国债回购", "质押式回购", "回购")

# 零现金股数变动：红股、送股、转增、新股入账等只增加持股，不发生任何现金收支
_FREE_SHARE_MARKERS = ("红股", "送股", "转增", "新股入账", "配股入账", "中签")

# 非交易现金流的金额列兜底：部分券商只给「成交金额」，红利/利息仍记在这里
_AMOUNT_HEADER_KEYWORDS = ("发生金额", "发生额", "收付金额", "成交金额", "成交额", "净额", "金额")


class StatementParseError(ValueError):
    """交割单无法解析；异常文案直接展示给用户。"""


@dataclass
class ParsedTrade:
    """清洗后的一笔标准成交。

    `commission_cents` 等费用是文件口径；系统计算口径下入账时会按费率重算，
    交割单口径下则直接采用这些真实费用与 `amount_cents`（发生金额）。
    """

    trade_date: date
    symbol: str
    side: str
    qty: int
    price_mills: int
    commission_cents: int = 0
    stamp_tax_cents: int = 0
    transfer_fee_cents: int = 0
    other_fee_cents: int = 0
    amount_cents: int | None = None      # 发生金额：交割单口径下的真实现金流
    name: str = ""
    source_row: int = 0

    @property
    def gross_cents(self) -> int:
        return qty_price_to_cents(self.qty, self.price_mills)

    @property
    def fee_total_cents(self) -> int:
        return (
            self.commission_cents
            + self.stamp_tax_cents
            + self.transfer_fee_cents
            + self.other_fee_cents
        )

    @property
    def statement_fee_cents(self) -> int | None:
        """按「发生金额」反推的真实总费用；没有发生金额时返回 None。"""
        if self.amount_cents is None:
            return None
        if self.side == SIDE_BUY:
            return max(0, abs(self.amount_cents) - self.gross_cents)
        return max(0, self.gross_cents - self.amount_cents)


@dataclass
class SkippedRow:
    """被隔离或无法识别的原始行，用于预览页解释「为什么没导入」。"""

    source_row: int
    reason: str
    values: list[str] = field(default_factory=list)


@dataclass
class ParsedCash:
    """交割单里的非交易现金流水：入账后成为账本的一笔现金分录。"""

    entry_date: date
    amount_cents: int          # 正为流入（红利、利息），负为流出（补扣红利税）
    kind: str                  # STATEMENT_DIVIDEND / STATEMENT_INTEREST / …
    memo: str = ""
    qty: int = 0               # 红利入账的成交数量：当次实际在册股数
    source_row: int = 0


@dataclass
class ParsedShare:
    """交割单里的零现金股数变动：红股入账、送股、转增、新股/配股入账。

    这类记录只增加持股数量、不发生现金收支，按 0 成本批次入账；否则持仓数量
    会比券商 APP 少，卖出时还会误报「可卖数量不足」。
    """

    entry_date: date
    symbol: str
    qty: int
    memo: str = ""
    source_row: int = 0


@dataclass
class ParseResult:
    source_path: str
    trades: list[ParsedTrade]
    skipped: list[SkippedRow]
    headers: list[str]
    mapping: dict[str, str]
    ignored_headers: list[str]
    cash_flows: list[ParsedCash] = field(default_factory=list)
    shares: list[ParsedShare] = field(default_factory=list)
    bank_transfer_cents: int = 0   # 银行转账合计
    repo_cents: int = 0            # 逆回购净现金流（含本金与利息）
    repo_outstanding_cents: int = 0  # 未到期逆回购本金：资金停放，仍计入可用资金
    repo_interest_cents: int = 0   # 逆回购利息收入
    final_balance_cents: int | None = None  # 末行「资金余额」，用于导入后自动对账
    share_balances: dict[str, int] = field(default_factory=dict)
    """股票余额：每个标的最新一行的在册股数，导入后与账面持仓对账。

    有这一列时可以直接读出「某个日期账户究竟持有多少股」，不需要靠买卖流水反推；
    若账面持仓与它不一致，说明账本缺少早期成交，应重新全量导入。
    """

    @property
    def non_trade_count(self) -> int:
        return sum(1 for row in self.skipped if row.reason.startswith("非交易记录"))

    @property
    def invalid_count(self) -> int:
        return len(self.skipped) - self.non_trade_count

    @property
    def income_flows(self) -> list[ParsedCash]:
        """红利/股息差异/利息等收益类流水（不含银行转账与逆回购）。"""
        return [
            flow
            for flow in self.cash_flows
            if flow.kind not in ("DEPOSIT", "WITHDRAW", "REPO")
        ]

    @property
    def cash_flow_count(self) -> int:
        return len(self.income_flows)

    @property
    def cash_flow_cents(self) -> int:
        return sum(item.amount_cents for item in self.income_flows)

    @property
    def trade_amount_count(self) -> int:
        """带有「发生金额」的成交笔数（交割单口径必须全部具备）。"""
        return sum(1 for trade in self.trades if trade.amount_cents is not None)


# ------------------------------------------------------------------ 表头嗅探
def _normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s\u3000:：()（）\[\]【】/\\]+", "", text)


def sniff_columns(header: list[Any]) -> dict[str, int]:
    """按关键词权重把表头映射为字段名 -> 列下标。

    采用全局「高分优先」的贪心分配：每列只归属一个字段，同一字段只取一列。
    例如「证券名称」对 ticker 有 30 分、对 name 有 60 分，因此不会被误判成代码列。
    """
    texts = [_normalize_header(cell) for cell in header]
    candidates: list[tuple[int, int, int, str]] = []
    for field_name, rules in _FIELD_RULES.items():
        negatives = _FIELD_NEGATIVES.get(field_name, ())
        for column, text in enumerate(texts):
            if not text:
                continue
            score = 0
            for keyword, weight in rules:
                lowered = keyword.lower()
                if lowered in text:
                    # 完全等于关键词时略加分，避免「证券名称」这类长列名误配
                    score = max(score, weight + (10 if text == lowered else 0))
            if score:
                score -= sum(weight for keyword, weight in negatives if keyword in text)
            if score > 0:
                candidates.append((score, _FIELD_ORDER.index(field_name), column, field_name))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    mapping: dict[str, int] = {}
    used_columns: set[int] = set()
    for _score, _priority, column, field_name in candidates:
        if field_name in mapping or column in used_columns:
            continue
        mapping[field_name] = column
        used_columns.add(column)
    return mapping


def locate_header(rows: list[list[Any]], scan_limit: int = _HEADER_SCAN_LIMIT) -> int | None:
    """在文件前若干行中定位真正的表头行，返回行下标。"""
    best: tuple[int, int] | None = None
    for index, row in enumerate(rows[:scan_limit]):
        if not row:
            continue
        non_empty = sum(1 for cell in row if not _is_blank(cell))
        if non_empty < 3:
            continue
        mapping = sniff_columns(row)
        if len(mapping) < 2:
            continue
        required = sum(1 for name in mapping if name in dict(_REQUIRED_MESSAGES))
        score = 100 * required + 10 * len(mapping)
        if best is None or score > best[0]:
            best = (score, index)
    return None if best is None else best[1]


# ------------------------------------------------------------------ 解析结果校验
# 代码形态：A 股 6 位、港股 5 位，或英文代码（美股 / 场内基金）
_PLAUSIBLE_SYMBOL_RE = re.compile(r"^(?:\d{5,6}|[A-Z][A-Z0-9.\-]{0,9})$")


def _validate_parsed(result: ParseResult) -> None:
    """发布前的最后一道闸：宁可拒绝导入，也不能把错列的数据写进账本。

    不同券商的表头排版差异很大，关键词匹配只能保证「大概率正确」。这里用
    数据本身的交叉关系复核：证券代码的形态、发生金额与「数量 × 价格」是否
    自洽。只要整体比例明显异常，就判定为列识别错位并中止导入。
    """
    trades = result.trades
    if not trades:
        # 纯现金流水交割单（逆回购 / 利息 / 红利）本来就没有买卖成交，跳过成交校验
        if (
            result.cash_flows
            or result.shares
            or result.repo_cents
            or result.bank_transfer_cents
        ):
            return
        raise StatementParseError(
            "没有解析出任何买卖成交。请确认交割单包含「证券代码、成交日期、"
            "买卖方向、成交数量、成交价格」五列，且表头没有被合并单元格拆散。"
        )

    bad_symbols = [trade for trade in trades if not _PLAUSIBLE_SYMBOL_RE.match(trade.symbol)]
    # 小文件没有统计意义，必须全部像代码；大文件允许极少量英文/特殊代码
    bad_symbol_limit = 0 if len(trades) <= 2 else max(2, len(trades) // 10)
    if len(bad_symbols) > bad_symbol_limit:
        first = bad_symbols[0]
        raise StatementParseError(
            f"证券代码列疑似识别错误：{len(bad_symbols)}/{len(trades)} 笔成交的代码不是"
            f"5~6 位数字或英文代码（如第 {first.source_row} 行「{first.symbol}」）。"
            "不同券商的表头排版可能不同，已停止导入以免写错账。"
        )

    checked = 0
    mismatches: list[str] = []
    for trade in trades:
        if trade.amount_cents is None:
            continue
        checked += 1
        gross = trade.gross_cents
        if gross <= 0:
            mismatches.append(f"第 {trade.source_row} 行成交额不为正")
            continue
        # 发生金额含费用，正常只差几元；3% + 10 元的宽容度仍能识别整列错位
        tolerance = max(1_000, gross * 3 // 100) + trade.fee_total_cents
        diff = abs(abs(trade.amount_cents) - gross)
        if diff > tolerance:
            mismatches.append(
                f"第 {trade.source_row} 行金额 {abs(trade.amount_cents) / 100:,.2f}"
                f" ≠ 数量×价格 {gross / 100:,.2f}"
            )

    # 全部成交都对不上，或超过 10% 的成交对不上，就说明不是个别脏数据
    if mismatches and (len(mismatches) == checked or len(mismatches) > max(2, checked // 10)):
        sample = "；".join(mismatches[:3])
        raise StatementParseError(
            f"检测到 {len(mismatches)}/{checked} 笔成交的「发生金额」与"
            f"「成交数量 × 成交价格」明显不符（{sample}），"
            "很可能是交割单列名排版不同导致列识别错位。已停止导入，"
            "请把这份交割单发给我们补充适配。"
        )


# ------------------------------------------------------------------ 读取
def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:10])
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in _DELIMITERS}
        winner = max(counts, key=counts.get)
        return winner if counts[winner] else ","


def _csv_rows(text: str) -> list[list[str]]:
    return [list(row) for row in csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))]


def _read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    fallback: list[list[str]] | None = None
    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        rows = _csv_rows(text)
        if locate_header(rows) is not None:
            return rows
        fallback = rows
    if fallback is None:
        raise StatementParseError("文件编码无法识别，请另存为 UTF-8 或 GBK 后重试")
    return fallback


def _read_xlsx(path: Path) -> list[list[Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的友好提示
        raise StatementParseError("读取 Excel 需要 openpyxl，请先安装：pip install openpyxl") from exc
    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _read_xls(path: Path) -> list[list[Any]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise StatementParseError("读取 .xls 需要 xlrd，请先安装：pip install xlrd") from exc
    workbook = xlrd.open_workbook(filename=str(path))
    sheet = workbook.sheet_by_index(0)
    return [sheet.row_values(index) for index in range(sheet.nrows)]


def read_table(path: str | Path) -> list[list[Any]]:
    """按扩展名读取交割单为二维表；未知扩展名按 CSV 处理。

    很多券商的「导出 xls」其实是 GBK 编码的制表符文本或 HTML，扩展名与真实格式
    不符。这里先看文件头：只有真正的 OLE / ZIP 二进制才走 Excel 解析，否则按文本读，
    避免 xlrd 抛出「Expected BOF record」这类让人摸不着头脑的错误。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise StatementParseError(f"文件不存在：{file_path}")
    suffix = file_path.suffix.lower()
    magic = file_path.read_bytes()[:8]
    if suffix in (".xlsx", ".xlsm"):
        rows = _read_xlsx(file_path) if magic[:2] == b"PK" else _read_csv(file_path)
    elif suffix == ".xls":
        rows = _read_xls(file_path) if magic == _OLE_MAGIC else _read_csv(file_path)
    else:
        rows = _read_csv(file_path)
    if not rows:
        raise StatementParseError("文件为空，或没有可读取的数据")
    return rows


# ------------------------------------------------------------------ 单元格清洗
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return str(value).strip()


def _is_blank(value: Any) -> bool:
    return _text(value) == ""


def _cell_at(row: list[Any], column: int | None) -> Any:
    if column is None or column >= len(row):
        return ""
    return row[column]


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _text(value).strip()
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    text = (
        text.replace(",", "")
        .replace("，", "")
        .replace(" ", "")
        .replace("元", "")
        .replace("¥", "")
        .replace("￥", "")
    )
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = Decimal(match.group(0))
    return -abs(number) if negative else number


def normalize_symbol(value: Any) -> str:
    """600000.SH / SH600000 / 600000.0 -> 600000；港股 5 位代码保留原样。

    Excel 把「000001」当数字读时前导零会丢失（变成 1），这里补齐 6 位，
    否则持仓会被记到一个叫「1」的虚假标的上。
    """
    text = _text(value).replace(" ", "")
    if not text:
        return ""
    match = re.search(r"\d{6}", text) or re.search(r"\d{5}", text)
    if match:
        return match.group(0)
    if re.fullmatch(r"\d{1,4}", text):
        return text.zfill(6)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", text):
        return text.upper()
    return text


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        # Excel 日期序列号（1900 起算，20000~80000 覆盖 1954~2119 年）
        if 20_000 <= number <= 80_000:
            return date(1899, 12, 30) + timedelta(days=int(number))
        text = _text(value)
    else:
        text = str(value).strip()
    if not text:
        return None
    text = text.split("T")[0]
    match = re.search(r"(\d{4})\s*[-/.年]?\s*(\d{1,2})\s*[-/.月]?\s*(\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_qty(value: Any) -> int | None:
    number = _decimal_value(value)
    if number is None or number <= 0:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def parse_price_mills(value: Any) -> int | None:
    number = _decimal_value(value)
    if number is None or number <= 0:
        return None
    return round_half_up(number * 1000)


def parse_cents(value: Any) -> int | None:
    number = _decimal_value(value)
    if number is None:
        return None
    return round_half_up(number * 100)


def detect_side(value: Any) -> str | None:
    text = _text(value).strip().upper().replace(" ", "")
    if not text:
        return None
    if text in ("S", "SELL", "2", "卖出", "证券卖出", "担保品卖出"):
        return SIDE_SELL
    if text in ("B", "BUY", "1", "买入", "证券买入", "担保品买入"):
        return SIDE_BUY
    # 先判卖出，避免「买卖」这类复合词被误判为买入
    if "卖出" in text or "卖" in text or "SELL" in text:
        return SIDE_SELL
    if "买入" in text or "买" in text or "BUY" in text:
        return SIDE_BUY
    return None


def _non_trade_description(text: str) -> str | None:
    for marker in _NON_TRADE_MARKERS:
        if marker.lower() in text.lower():
            return marker
    return None


def _matches_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.lower() in text.lower() for marker in markers)


def _cash_kind(text: str) -> str:
    """非交易现金流水的入账类型；名称与账本内生成的分录区分开，便于追溯来源。"""
    lowered = text.lower()
    if "红利" in lowered or "股息" in lowered or "分红" in lowered:
        if "差异" in lowered or "税" in lowered:
            return "STATEMENT_DIVIDEND_TAX"
        return "STATEMENT_DIVIDEND"
    if "利息" in lowered:
        return "STATEMENT_INTEREST"
    return "STATEMENT_CASH"


def _repo_key(memo: str) -> str:
    """逆回购品种键：优先用备注里的 6 位回购代码，没有才退回整条备注。

    不同券商对开仓/到期的备注写法并不一致（有的带委托号），只按备注全等匹配会
    认不出同一笔回购，利息就被静默丢掉。
    """
    match = re.search(r"\d{6}", memo)
    return match.group(0) if match else memo


def _split_repo_flows(flows: list[ParsedCash]) -> tuple[list[ParsedCash], int, int]:
    """把逆回购拆成「本金停放」与「利息收入」。

    逆回购本金仍属于可用资金，不能当成出金记入现金；只有到期利息是真实收益。
    开仓（金额为负）不计现金流水，只登记未结本金；到期（金额为正）核销同一品种
    的未结本金，差额作为利息补入现金。若期初已持有回购、交割单里没有对应开仓行，
    本金在开仓时已按资金停放计入可用资金，不能再记一笔流入；只把超出本金的差额
    作为利息入账（回购本金必为 1000 元的整数倍，可据此反推）。
    """
    open_principal: dict[str, list[int]] = {}
    result: list[ParsedCash] = []
    outstanding = 0
    interest_total = 0

    def _pick(groups: list[tuple[str, list[int]]]) -> tuple[str, int] | None:
        """在候选未结本金里挑差额最小的一笔；优先利息区间合理（≤10 元）的。"""
        best: tuple[tuple[int, int], str, int] | None = None
        for key, principals in groups:
            for principal in principals:
                delta = inflow - principal
                if delta < 0:
                    continue
                score = (0 if delta <= 1000 else 1, delta)
                if best is None or score < best[0]:
                    best = (score, key, principal)
        return None if best is None else (best[1], best[2])

    for flow in flows:
        if flow.kind != "REPO":
            result.append(flow)
            continue
        if flow.amount_cents < 0:
            principal = -flow.amount_cents
            open_principal.setdefault(_repo_key(flow.memo), []).append(principal)
            outstanding += principal
            continue

        inflow = flow.amount_cents
        key = _repo_key(flow.memo)
        match = _pick([(key, open_principal.get(key, []))])
        if match is None and open_principal:
            match = _pick(list(open_principal.items()))
        if match is None:
            # 期初已持有回购：交割单里没有开仓行，本金不能重复计入现金，
            # 只有超出本金的差额是利息。本金必为 1000 元（100000 分）的整数倍。
            principal = (inflow // 100_000) * 100_000
            interest = inflow - principal
            if interest > 0:
                interest_total += interest
                result.append(
                    ParsedCash(
                        entry_date=flow.entry_date,
                        amount_cents=interest,
                        kind="STATEMENT_INTEREST",
                        memo=f"{flow.memo} 回购利息",
                        source_row=flow.source_row,
                    )
                )
            continue

        mat_key, principal = match
        open_principal[mat_key].remove(principal)
        if not open_principal[mat_key]:
            open_principal.pop(mat_key, None)
        outstanding -= principal
        interest = inflow - principal
        if interest > 0:
            interest_total += interest
            result.append(
                ParsedCash(
                    entry_date=flow.entry_date,
                    amount_cents=interest,
                    kind="STATEMENT_INTEREST",
                    memo=f"{flow.memo} 回购利息",
                    source_row=flow.source_row,
                )
            )
    return result, outstanding, interest_total


# ------------------------------------------------------------------ 主流程
def parse_statement(path: str | Path) -> ParseResult:
    """解析一份券商交割单，返回标准成交与被隔离记录。

    Raises:
        StatementParseError: 找不到表头，或缺少证券代码/日期/方向/数量/价格之一。
    """
    rows = read_table(path)
    header_index = locate_header(rows)
    if header_index is None:
        raise StatementParseError(
            "未能找到交割单表头行，请检查文件是否包含「证券代码、成交日期、"
            "买卖方向、成交数量、成交价格」等列"
        )

    header = [_text(cell) for cell in rows[header_index]]
    mapping = sniff_columns(rows[header_index])
    for field_name, message in _REQUIRED_MESSAGES:
        if field_name not in mapping:
            raise StatementParseError(message)

    ignored_headers = [
        text for column, text in enumerate(header)
        if text and column not in mapping.values()
    ]

    trades: list[ParsedTrade] = []
    skipped: list[SkippedRow] = []
    cash_flows: list[ParsedCash] = []
    shares: list[ParsedShare] = []
    bank_transfer_cents = 0
    repo_cents = 0
    final_balance_cents: int | None = None
    share_balances: dict[str, int] = {}

    # 成交日期/发生日期列之外的日期列（如「交收日期」）：红利到账行常常只填交收日
    date_columns = [
        column for column, text in enumerate(header)
        if text and ("日期" in text or "时间" in text or "date" in text.lower())
    ]
    # 金额列兜底：部分券商没有「发生金额」，红利/利息仍记在「成交金额」里
    amount_columns = [
        column for column, text in enumerate(header)
        if text and any(key in text for key in _AMOUNT_HEADER_KEYWORDS)
    ]

    def row_date(raw_row: list[Any]) -> date | None:
        primary = mapping.get("date")
        if primary is not None:
            parsed = parse_date(_cell_at(raw_row, primary))
            if parsed is not None:
                return parsed
        for column in date_columns:
            if column == primary:
                continue
            parsed = parse_date(_cell_at(raw_row, column))
            if parsed is not None:
                return parsed
        return None

    def row_amount(raw_row: list[Any]) -> int | None:
        primary = mapping.get("amount")
        if primary is not None:
            parsed = parse_cents(_cell_at(raw_row, primary))
            if parsed is not None:
                return parsed
        for column in amount_columns:
            if column == primary:
                continue
            parsed = parse_cents(_cell_at(raw_row, column))
            if parsed is not None:
                return parsed
        return None

    for index in range(header_index + 1, len(rows)):
        raw_row = rows[index]
        if all(_is_blank(cell) for cell in raw_row):
            continue
        source_row = index + 1
        values = [_text(cell) for cell in raw_row]

        if "balance" in mapping:
            balance = parse_cents(_cell_at(raw_row, mapping["balance"]))
            if balance is not None:
                final_balance_cents = balance

        action_text = _text(_cell_at(raw_row, mapping.get("action"))).strip()
        marker = _non_trade_description(action_text)
        if marker is None and not action_text:
            # 方向列空白时，再从整行里找「银行转证券 / 红利入账」等字样
            marker = _non_trade_description(" ".join(values))
        if marker is not None:
            skipped.append(
                SkippedRow(
                    source_row=source_row,
                    reason=f"非交易记录：{action_text or marker}",
                    values=values,
                )
            )
            # 交割单里的真实现金流（红利、股息差异、利息等）必须补进账本：
            # 只重放成交时，分红到账与利息结息缺席会让现金在中途见底。
            flow_text = action_text or " ".join(values)
            share_marker = next(
                (item for item in _FREE_SHARE_MARKERS if item in flow_text), None
            )
            if share_marker is not None:
                share_qty = parse_qty(_cell_at(raw_row, mapping.get("qty")))
                share_symbol = normalize_symbol(_cell_at(raw_row, mapping.get("ticker")))
                share_date = row_date(raw_row)
                if share_qty and share_symbol and share_date is not None:
                    # 红股/送转/新股入账：只增加持股，按 0 成本批次入账
                    shares.append(
                        ParsedShare(
                            entry_date=share_date,
                            symbol=share_symbol,
                            qty=share_qty,
                            memo=flow_text,
                            source_row=source_row,
                        )
                    )
                    continue
            amount_cents = row_amount(raw_row)
            flow_date = row_date(raw_row)
            if amount_cents and flow_date is not None:
                symbol = normalize_symbol(_cell_at(raw_row, mapping.get("ticker")))
                name = _text(_cell_at(raw_row, mapping.get("name"))).strip()
                memo = " ".join(
                    part for part in (action_text or marker, symbol, name) if part
                )
                if _matches_any(flow_text, _TRANSFER_MARKERS):
                    bank_transfer_cents += amount_cents
                    kind = "DEPOSIT" if amount_cents > 0 else "WITHDRAW"
                elif _matches_any(flow_text, _REPO_MARKERS):
                    repo_cents += amount_cents
                    kind = "REPO"
                else:
                    kind = _cash_kind(flow_text)
                cash_flows.append(
                    ParsedCash(
                        entry_date=flow_date,
                        amount_cents=amount_cents,
                        kind=kind,
                        memo=memo,
                        qty=parse_qty(_cell_at(raw_row, mapping.get("qty"))) or 0,
                        source_row=source_row,
                    )
                )
            continue

        side = detect_side(action_text)
        if side is None:
            skipped.append(
                SkippedRow(
                    source_row=source_row,
                    reason=f"无法识别买卖方向：{action_text or '空'}",
                    values=values,
                )
            )
            continue

        symbol = normalize_symbol(_cell_at(raw_row, mapping.get("ticker")))
        if not symbol:
            skipped.append(
                SkippedRow(source_row=source_row, reason="证券代码为空", values=values)
            )
            continue

        raw_date = _cell_at(raw_row, mapping.get("date"))
        trade_date = row_date(raw_row)
        if trade_date is None:
            skipped.append(
                SkippedRow(
                    source_row=source_row,
                    reason=f"成交日期无法识别：{_text(raw_date) or '空'}",
                    values=values,
                )
            )
            continue

        raw_qty = _cell_at(raw_row, mapping.get("qty"))
        qty = parse_qty(raw_qty)
        if qty is None:
            skipped.append(
                SkippedRow(
                    source_row=source_row,
                    reason=f"成交数量无法识别：{_text(raw_qty) or '空'}",
                    values=values,
                )
            )
            continue

        raw_price = _cell_at(raw_row, mapping.get("price"))
        price_mills = parse_price_mills(raw_price)
        if price_mills is None:
            skipped.append(
                SkippedRow(
                    source_row=source_row,
                    reason=f"成交价格无法识别：{_text(raw_price) or '空'}",
                    values=values,
                )
            )
            continue

        def _abs_cents(field_name: str) -> int:
            if field_name not in mapping:
                return 0
            parsed = parse_cents(_cell_at(raw_row, mapping[field_name]))
            return abs(parsed) if parsed is not None else 0

        commission_cents = _abs_cents("commission")
        stamp_tax_cents = _abs_cents("stamp")
        transfer_fee_cents = _abs_cents("transfer")
        other_fee_cents = _abs_cents("other_fee")
        amount_cents = (
            parse_cents(_cell_at(raw_row, mapping["amount"]))
            if "amount" in mapping
            else None
        )

        name = _text(_cell_at(raw_row, mapping.get("name"))).strip()
        # 股票余额是券商口径的「成交后在册股数」，按文件行序取最后一笔就是当前持仓
        if "share_balance" in mapping:
            balance_qty = parse_qty(_cell_at(raw_row, mapping["share_balance"]))
            if balance_qty is not None:
                share_balances[symbol] = balance_qty
        trades.append(
            ParsedTrade(
                trade_date=trade_date,
                symbol=symbol,
                side=side,
                qty=qty,
                price_mills=price_mills,
                commission_cents=commission_cents,
                stamp_tax_cents=stamp_tax_cents,
                transfer_fee_cents=transfer_fee_cents,
                other_fee_cents=other_fee_cents,
                amount_cents=amount_cents,
                name=name,
                source_row=source_row,
            )
        )

    cash_flows, repo_outstanding, repo_interest = _split_repo_flows(cash_flows)
    result = ParseResult(
        source_path=str(path),
        trades=trades,
        skipped=skipped,
        headers=header,
        mapping={
            field_name: header[column] if column < len(header) else ""
            for field_name, column in mapping.items()
        },
        ignored_headers=ignored_headers,
        cash_flows=cash_flows,
        shares=shares,
        bank_transfer_cents=bank_transfer_cents,
        repo_cents=repo_cents,
        repo_outstanding_cents=repo_outstanding,
        repo_interest_cents=repo_interest,
        final_balance_cents=final_balance_cents,
        share_balances=share_balances,
    )
    _validate_parsed(result)
    return result
