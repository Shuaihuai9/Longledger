"""行情服务：不依赖任何 UI 框架的纯 Python API。

对外只暴露 `get_daily_ohlc(ticker, day)`：取单日「前收盘 / 开 / 高 / 低 / 收」。
设计约束：
- 只取**不复权**价格（真实成本口径）；
- 非交易日、停牌、代码非法、网络失败一律返回带 `error` 的结果对象，绝不抛未知异常；
- 可用 `allow_previous_trading_day` 控制是否回退到最近一个有行情的交易日，
  回退时用 `price_date` 标记实际日期、`is_exact_date=False`。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from core.money import mills_to_yuan
from ingest.quotes_download import QuoteDownloadError, fetch_ohlc_bars, parse_symbol

DEFAULT_LOOKBACK_DAYS = 45


@dataclass(frozen=True)
class DailyOhlc:
    """单日行情结果。`error` 非空表示未取到可用行情。"""

    ticker: str
    requested_date: date
    price_date: date | None = None
    prev_close_mills: int | None = None
    open_mills: int | None = None
    high_mills: int | None = None
    low_mills: int | None = None
    close_mills: int | None = None
    is_exact_date: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.close_mills is not None

    @property
    def prev_close(self):
        """前收盘价（元），无数据时返回 None。"""
        return None if self.prev_close_mills is None else mills_to_yuan(self.prev_close_mills)

    @property
    def open(self):
        return None if self.open_mills is None else mills_to_yuan(self.open_mills)

    @property
    def high(self):
        return None if self.high_mills is None else mills_to_yuan(self.high_mills)

    @property
    def low(self):
        return None if self.low_mills is None else mills_to_yuan(self.low_mills)

    @property
    def close(self):
        return None if self.close_mills is None else mills_to_yuan(self.close_mills)

    def as_dict(self) -> dict:
        """供其他前端（PyQt / Electron）直接序列化的扁平结构。"""
        data = asdict(self)
        data["requested_date"] = self.requested_date.isoformat()
        data["price_date"] = self.price_date.isoformat() if self.price_date else None
        data["ok"] = self.ok
        for field in ("prev_close", "open", "high", "low", "close"):
            mills = {
                "prev_close": self.prev_close_mills,
                "open": self.open_mills,
                "high": self.high_mills,
                "low": self.low_mills,
                "close": self.close_mills,
            }[field]
            data[f"{field}_yuan"] = None if mills is None else str(mills_to_yuan(mills))
        return data


def _coerce_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def get_daily_ohlc(
    ticker: str,
    day,
    *,
    allow_previous_trading_day: bool = True,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    timeout: float = 10,
) -> DailyOhlc:
    """取某标的某日的不复权 OHLC。

    当日无数据时：`allow_previous_trading_day=True` 回退到最近交易日并标记
    `price_date`；否则返回明确错误。任何情况下都不抛出未捕获异常。
    """
    requested = _coerce_date(day)
    if requested is None:
        return DailyOhlc(ticker=str(ticker), requested_date=date.today(), error="日期格式无法识别")

    try:
        parse_symbol(ticker)
    except ValueError as exc:
        return DailyOhlc(ticker=str(ticker), requested_date=requested, error=str(exc))

    start = requested - timedelta(days=max(1, int(lookback_days)))
    try:
        _, bars = fetch_ohlc_bars(ticker, start, requested, timeout)
    except QuoteDownloadError as exc:
        return DailyOhlc(ticker=str(ticker), requested_date=requested, error=str(exc))
    except ValueError as exc:
        return DailyOhlc(ticker=str(ticker), requested_date=requested, error=str(exc))
    except Exception as exc:  # 兜底：任何意外都转成可展示的错误
        return DailyOhlc(
            ticker=str(ticker), requested_date=requested, error=f"行情获取异常：{exc}"
        )

    usable = [bar for bar in bars if bar.price_date <= requested]
    if not usable:
        return DailyOhlc(
            ticker=str(ticker),
            requested_date=requested,
            error=f"{requested.isoformat()} 之前无可用户线数据（可能尚未上市）",
        )

    target = usable[-1]
    if target.price_date != requested and not allow_previous_trading_day:
        return DailyOhlc(
            ticker=str(ticker),
            requested_date=requested,
            price_date=target.price_date,
            error=(
                f"{requested.isoformat()} 无交易（非交易日或停牌），"
                f"最近交易日为 {target.price_date.isoformat()}"
            ),
        )

    index = bars.index(target)
    prev_close = bars[index - 1].close_mills if index > 0 else None
    return DailyOhlc(
        ticker=str(ticker),
        requested_date=requested,
        price_date=target.price_date,
        prev_close_mills=prev_close,
        open_mills=target.open_mills,
        high_mills=target.high_mills,
        low_mills=target.low_mills,
        close_mills=target.close_mills,
        is_exact_date=target.price_date == requested,
    )


def list_quote_coverage(conn) -> list[dict]:
    """本地行情覆盖情况：证券代码、行数、起止日期与最新收盘（毫）。"""
    from data.repositories import Repository

    return Repository(conn).quote_coverage()


def get_prev_close_mills(ticker: str, ex_date, **kwargs) -> int | None:
    """除权除息用的前收盘价（毫）：取除权日前一个交易日的收盘。

    除权日若停牌/非交易日，退化为「最近一个早于除权日的交易日收盘」。
    """
    target_day = _coerce_date(ex_date)
    if target_day is None:
        return None
    quote = get_daily_ohlc(ticker, target_day, allow_previous_trading_day=True, **kwargs)
    if not quote.ok:
        return None
    if quote.price_date is not None and quote.price_date < target_day:
        # 除权日本身没有行情：最近交易日收盘即为前收盘
        return quote.close_mills
    return quote.prev_close_mills
