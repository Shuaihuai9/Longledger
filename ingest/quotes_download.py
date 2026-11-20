"""主动下载 A 股 / ETF 日线收盘价。

数据源优先级：
1. 东方财富 push2his 日 K 接口（JSON，覆盖沪深个股与场内基金）
2. 腾讯行情 gtimg 日 K 接口（JSON，作为兜底）

设计口径：
- 只取**不复权**收盘价。账本按真实成交价与现金流记账，复权价会破坏持仓市值
  与历史现金的对应关系；除权缺口由公司行为模块调整股数与成本来消化。
- 价格统一转成「毫」（1 元 = 1000 毫）后写入 quotes 表。
- 不引入第三方网络库，仅用标准库 urllib；失败信息逐源回报，便于排查。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.money import yuan_to_mills

EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

# 东财日线单次请求最多返回 2000 根 K 线；更长区间需按 end 向前分页
_PAGE_LIMIT = 24  # 24 × 2000 根 ≈ 90 年，足以覆盖任何上市历史

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_SYMBOL_PATTERN = re.compile(
    r"^(?:(sh|sz)[.\-_]?)?(\d{6})(?:[.\-_]?(sh|sz))?$", re.IGNORECASE
)
_SPLIT_PATTERN = re.compile(r"[,，;；\s]+")

_SH_FIRST = set("569")
_SZ_FIRST = set("0123")


class QuoteDownloadError(RuntimeError):
    """行情下载失败（网络、解析或无数据）。"""


@dataclass(frozen=True)
class SymbolRef:
    symbol: str          # 6 位数字代码
    market: int          # 东方财富 secid 市场位：1=沪，0=深

    @property
    def exchange(self) -> str:
        return "sh" if self.market == 1 else "sz"


@dataclass(frozen=True)
class Bar:
    price_date: date
    close_mills: int


@dataclass(frozen=True)
class OhlcBar:
    """单日完整行情（不复权）。价格单位毫，供成本与除权计算使用。"""

    price_date: date
    open_mills: int
    high_mills: int
    low_mills: int
    close_mills: int


@dataclass(frozen=True)
class DownloadResult:
    symbol: str
    rows: int = 0
    first_date: date | None = None
    last_date: date | None = None
    error: str | None = None


def split_symbols(text: str) -> list[str]:
    """把逗号 / 分号 / 空格 / 换行分隔的代码串拆成列表。"""
    return [part for part in _SPLIT_PATTERN.split(text or "") if part]


def parse_symbol(raw: str) -> SymbolRef:
    """识别 600000、sh600000、600000.SH 等写法，并推断沪深市场。"""
    text = str(raw).strip().upper().replace(" ", "")
    if not text:
        raise ValueError("代码不能为空")
    match = _SYMBOL_PATTERN.match(text)
    if not match:
        raise ValueError(f"无法识别的代码：{raw}")
    prefix, code, suffix = match.groups()
    hint = (prefix or suffix or "").lower()

    if hint == "sh":
        market = 1
    elif hint == "sz":
        market = 0
    elif code[0] in _SH_FIRST:
        market = 1
    elif code[0] in _SZ_FIRST:
        market = 0
    else:
        raise ValueError(f"暂不支持该代码段：{code}（仅支持沪深 A 股与场内基金）")
    return SymbolRef(symbol=code, market=market)


def fetch_security_name(symbol: str, timeout: float = 10) -> str | None:
    """取证券简称；多源逐级兜底，取不到返回 None。

    1. 东财实时报价 f58（需 ut/fltt 参数，否则 data 为空）
    2. 东财日线接口的 data.name（下载行情所用的同一接口，稳定）
    3. 腾讯简版行情 qt.gtimg.cn（GBK 文本）
    """
    ref = parse_symbol(symbol)

    try:
        payload = _get_json(
            EASTMONEY_QUOTE_URL,
            {
                "secid": f"{ref.market}.{ref.symbol}",
                "fields": "f57,f58",
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "invt": "2",
                "fltt": "2",
            },
            referer="https://quote.eastmoney.com/",
            timeout=timeout,
        )
        name = str((payload.get("data") or {}).get("f58") or "").strip()
        if name:
            return name
    except Exception:
        pass

    try:
        payload = _get_json(
            EASTMONEY_URL,
            {
                "secid": f"{ref.market}.{ref.symbol}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "0",
                "beg": "0",
                "end": "20500101",
                "lmt": "10",
            },
            referer="https://quote.eastmoney.com/",
            timeout=timeout,
        )
        name = str((payload.get("data") or {}).get("name") or "").strip()
        if name:
            return name
    except Exception:
        pass

    try:
        raw = _get_text(
            f"{TENCENT_QUOTE_URL}{ref.exchange}{ref.symbol}",
            referer="https://gu.qq.com/",
            timeout=timeout,
        )
        # v_sh601398="1~工商银行~601398~..."
        match = re.search(r'="([^"]+)"', raw)
        if match:
            fields = match.group(1).split("~")
            if len(fields) > 1 and fields[1].strip():
                return fields[1].strip()
    except Exception:
        pass

    return None


def _get_json(url: str, params: dict, referer: str, timeout: float) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(
        full_url,
        headers={
            "User-Agent": _UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (URLError, OSError) as exc:
        raise QuoteDownloadError(f"网络请求失败：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise QuoteDownloadError("数据源返回内容不是合法 JSON") from exc


def _get_text(url: str, referer: str, timeout: float) -> str:
    request = Request(
        url,
        headers={"User-Agent": _UA, "Referer": referer, "Accept": "*/*"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (URLError, OSError) as exc:
        raise QuoteDownloadError(f"网络请求失败：{exc}") from exc
    return raw.decode("gbk", errors="replace")


def _paginate(fetch_once, start: date, end: date, label: str) -> list:
    """按 end 向前回溯分页，直到覆盖 start 或数据取尽。

    数据源单次最多返回 2000 根 K 线，一次性请求长区间会被静默截断
    （表现为「只下载到 2018 年」），因此必须分页拼接。
    """
    merged: dict[date, object] = {}
    cursor_end = end
    for _ in range(_PAGE_LIMIT):
        chunk = fetch_once(cursor_end)
        if not chunk:
            break
        for bar in chunk:
            merged[bar.price_date] = bar
        earliest = min(bar.price_date for bar in chunk)
        if earliest <= start:
            break
        next_end = earliest - timedelta(days=1)
        if next_end >= cursor_end:
            break
        cursor_end = next_end
    if not merged:
        raise QuoteDownloadError(f"{label}未返回日线数据")
    return [merged[day] for day in sorted(merged)]


def _parse_eastmoney(payload: dict) -> list[Bar]:
    """东方财富 klines 行：日期,开,收,高,低,成交量,... 取第 3 列收盘价。"""
    data = payload.get("data") or {}
    lines = data.get("klines") or []
    bars: list[Bar] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 3:
            continue
        try:
            day = date.fromisoformat(parts[0])
            close = yuan_to_mills(parts[2])
        except (ValueError, ArithmeticError):
            continue
        bars.append(Bar(price_date=day, close_mills=close))
    return bars


def _parse_tencent(payload: dict, code: str) -> list[Bar]:
    """腾讯行格式：[日期, 开, 收, 高, 低, 成交量, ...]，取第 3 项收盘价。"""
    data = payload.get("data") or {}
    node = None
    for key in (code, code.lower(), code.upper()):
        if isinstance(data.get(key), dict):
            node = data[key]
            break
    if node is None:
        return []

    rows = None
    for field in ("day", "qfqday", "hfqday"):
        if node.get(field):
            rows = node[field]
            break
    if not rows:
        return []

    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        try:
            day = date.fromisoformat(str(row[0])[:10])
            close = yuan_to_mills(str(row[2]))
        except (ValueError, ArithmeticError):
            continue
        bars.append(Bar(price_date=day, close_mills=close))
    return bars


def _fetch_from_eastmoney(
    ref: SymbolRef, start: date, end: date, timeout: float
) -> list[Bar]:
    def fetch_once(batch_end: date) -> list[Bar]:
        payload = _get_json(
            EASTMONEY_URL,
            {
                "secid": f"{ref.market}.{ref.symbol}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",   # 日线
                "fqt": "0",     # 不复权
                "beg": start.strftime("%Y%m%d"),
                "end": batch_end.strftime("%Y%m%d"),
                "lmt": "1000000",
            },
            referer="https://quote.eastmoney.com/",
            timeout=timeout,
        )
        return _parse_eastmoney(payload)

    return _paginate(fetch_once, start, end, "东方财富")


def _fetch_from_tencent(
    ref: SymbolRef, start: date, end: date, timeout: float
) -> list[Bar]:
    code = f"{ref.exchange}{ref.symbol}"

    def fetch_once(batch_end: date) -> list[Bar]:
        payload = _get_json(
            TENCENT_URL,
            {"param": f"{code},day,{start.isoformat()},{batch_end.isoformat()},2000,"},
            referer="https://gu.qq.com/",
            timeout=timeout,
        )
        return _parse_tencent(payload, code)

    return _paginate(fetch_once, start, end, "腾讯行情")


def _normalize(bars: list[Bar]) -> list[Bar]:
    """按日期排序并按日期去重（后到的覆盖先到的）。"""
    merged: dict[date, Bar] = {}
    for bar in bars:
        merged[bar.price_date] = bar
    return [merged[day] for day in sorted(merged)]


def _parse_eastmoney_ohlc(payload: dict) -> list[OhlcBar]:
    """东财行：日期,开,收,高,低,... —— 取开 / 高 / 低 / 收四价。"""
    data = payload.get("data") or {}
    lines = data.get("klines") or []
    bars: list[OhlcBar] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 5:
            continue
        try:
            bars.append(
                OhlcBar(
                    price_date=date.fromisoformat(parts[0]),
                    open_mills=yuan_to_mills(parts[1]),
                    close_mills=yuan_to_mills(parts[2]),
                    high_mills=yuan_to_mills(parts[3]),
                    low_mills=yuan_to_mills(parts[4]),
                )
            )
        except (ValueError, ArithmeticError):
            continue
    return bars


def _parse_tencent_ohlc(payload: dict, code: str) -> list[OhlcBar]:
    """腾讯行：[日期, 开, 收, 高, 低, 成交量, ...]。"""
    data = payload.get("data") or {}
    node = None
    for key in (code, code.lower(), code.upper()):
        if isinstance(data.get(key), dict):
            node = data[key]
            break
    if node is None:
        return []

    rows = None
    for field in ("day", "qfqday", "hfqday"):
        if node.get(field):
            rows = node[field]
            break
    if not rows:
        return []

    bars: list[OhlcBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            bars.append(
                OhlcBar(
                    price_date=date.fromisoformat(str(row[0])[:10]),
                    open_mills=yuan_to_mills(str(row[1])),
                    close_mills=yuan_to_mills(str(row[2])),
                    high_mills=yuan_to_mills(str(row[3])),
                    low_mills=yuan_to_mills(str(row[4])),
                )
            )
        except (ValueError, ArithmeticError):
            continue
    return bars


def _fetch_ohlc_eastmoney(
    ref: SymbolRef, start: date, end: date, timeout: float
) -> list[OhlcBar]:
    def fetch_once(batch_end: date) -> list[OhlcBar]:
        payload = _get_json(
            EASTMONEY_URL,
            {
                "secid": f"{ref.market}.{ref.symbol}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",   # 日线
                "fqt": "0",     # 不复权
                "beg": start.strftime("%Y%m%d"),
                "end": batch_end.strftime("%Y%m%d"),
                "lmt": "1000000",
            },
            referer="https://quote.eastmoney.com/",
            timeout=timeout,
        )
        return _parse_eastmoney_ohlc(payload)

    return _paginate(fetch_once, start, end, "东方财富")


def _fetch_ohlc_tencent(
    ref: SymbolRef, start: date, end: date, timeout: float
) -> list[OhlcBar]:
    code = f"{ref.exchange}{ref.symbol}"

    def fetch_once(batch_end: date) -> list[OhlcBar]:
        payload = _get_json(
            TENCENT_URL,
            {"param": f"{code},day,{start.isoformat()},{batch_end.isoformat()},2000,"},
            referer="https://gu.qq.com/",
            timeout=timeout,
        )
        return _parse_tencent_ohlc(payload, code)

    return _paginate(fetch_once, start, end, "腾讯行情")


def fetch_ohlc_bars(
    raw_symbol: str, start: date, end: date, timeout: float = 10
) -> tuple[SymbolRef, list[OhlcBar]]:
    """下载单标的日线 OHLC（不复权），东财主源、腾讯兜底。"""
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    ref = parse_symbol(raw_symbol)

    failures: list[str] = []
    for label, fetcher in (
        ("东方财富", _fetch_ohlc_eastmoney),
        ("腾讯行情", _fetch_ohlc_tencent),
    ):
        try:
            bars = fetcher(ref, start, end, timeout)
            if bars:
                merged = {bar.price_date: bar for bar in bars}
                return ref, [merged[day] for day in sorted(merged)]
            failures.append(f"{label}：无数据")
        except Exception as exc:  # 单源异常不阻断兜底源
            failures.append(f"{label}：{exc}")
    raise QuoteDownloadError(f"{ref.symbol} 行情下载失败（" + "；".join(failures) + "）")


def fetch_daily_closes(
    raw_symbol: str, start: date, end: date, timeout: float = 10
) -> tuple[SymbolRef, list[Bar]]:
    """下载单个标的的日线收盘价；主源失败或为空时自动切换兜底源。"""
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    ref = parse_symbol(raw_symbol)

    failures: list[str] = []
    for label, fetcher in (
        ("东方财富", _fetch_from_eastmoney),
        ("腾讯行情", _fetch_from_tencent),
    ):
        try:
            bars = _normalize(fetcher(ref, start, end, timeout))
            if bars:
                return ref, bars
            failures.append(f"{label}：无数据")
        except Exception as exc:  # 单源异常不阻断兜底源
            failures.append(f"{label}：{exc}")
    raise QuoteDownloadError(
        f"{ref.symbol} 下载失败（" + "；".join(failures) + "）"
    )


def download_history(
    repo, raw_symbols: list[str], start: date, end: date, timeout: float = 10
) -> list[DownloadResult]:
    """批量下载并写入 quotes 表；单个标的失败不影响其他标的。"""
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")

    results: list[DownloadResult] = []
    seen: set[str] = set()
    with repo.lock():
        for raw in raw_symbols:
            key = str(raw).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                ref, bars = fetch_daily_closes(key, start, end, timeout)
                for bar in bars:
                    repo.upsert_quote(ref.symbol, bar.price_date, bar.close_mills)
                repo.commit()
                results.append(
                    DownloadResult(
                        symbol=ref.symbol,
                        rows=len(bars),
                        first_date=bars[0].price_date,
                        last_date=bars[-1].price_date,
                    )
                )
            except Exception as exc:
                repo.rollback()
                results.append(DownloadResult(symbol=key, error=str(exc)))
    return results
