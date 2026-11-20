"""下载 A 股历史分红送转事件（东方财富数据中心）。

口径说明：
- `PRETAX_BONUS_RMB` 是「每 10 股派息（税前，元）」，换算为每股毫：值 × 100。
- 送股比例取 `BONUS_RATIO`（每 10 股送股）；缺失时用
  `BONUS_IT_RATIO - IT_RATIO` 推导（前者是送转合计）。
- 转增比例取 `IT_RATIO`（每 10 股转增）。
- ETF（场内基金）不在该报表覆盖范围内，接口会返回空结果；此时应使用
  手动录入接口 `services.corporate_actions_service.record_manual_dividend`。

本模块只负责「取数 + 解析」，不写数据库、不依赖任何 UI 框架。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.money import yuan_to_mills

DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class DividendFetchError(RuntimeError):
    """分红数据下载失败（网络或报文异常）。"""


@dataclass(frozen=True)
class DividendEvent:
    """一条已实施的除权除息事件。比例均为「每股」口径。"""

    ticker: str
    ex_date: date
    cash_per_share_mills: int
    bonus_ratio: Decimal
    transfer_ratio: Decimal
    pay_date: date | None = None


def _to_decimal(value) -> Decimal:
    if value in (None, "", "-"):
        return Decimal("0")
    return Decimal(str(value))


def _parse_day(value) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _ratios(row: dict) -> tuple[Decimal, Decimal]:
    """把「每 10 股」口径换算成「每股」比例。"""
    bonus = row.get("BONUS_RATIO")
    if bonus is None:
        bonus = _to_decimal(row.get("BONUS_IT_RATIO")) - _to_decimal(row.get("IT_RATIO"))
    bonus_ratio = _to_decimal(bonus) / Decimal(10)
    transfer_ratio = _to_decimal(row.get("IT_RATIO")) / Decimal(10)
    if bonus_ratio < 0:
        bonus_ratio = Decimal("0")
    if transfer_ratio < 0:
        transfer_ratio = Decimal("0")
    return bonus_ratio, transfer_ratio


def parse_eastmoney_dividends(payload: dict, ticker: str) -> list[DividendEvent]:
    """解析东财 RPT_SHAREBONUS_DET 报文；坏行跳过，不抛异常。"""
    result = payload.get("result") or {}
    rows = result.get("data") or []
    events: dict[tuple, DividendEvent] = {}
    for row in rows:
        ex_date = _parse_day(row.get("EX_DIVIDEND_DATE"))
        if ex_date is None:
            continue
        cash_mills = yuan_to_mills(_to_decimal(row.get("PRETAX_BONUS_RMB")) / Decimal(10))
        bonus_ratio, transfer_ratio = _ratios(row)
        if cash_mills <= 0 and bonus_ratio <= 0 and transfer_ratio <= 0:
            continue
        event = DividendEvent(
            ticker=str(ticker),
            ex_date=ex_date,
            cash_per_share_mills=cash_mills,
            bonus_ratio=bonus_ratio,
            transfer_ratio=transfer_ratio,
            pay_date=_parse_day(row.get("DIVIDEND_DATE")) or ex_date,
        )
        # 同日可能有多条（如送转与派息分行披露），后者覆盖前者前先按日期聚合
        events[(event.ex_date, event.cash_per_share_mills,
                event.bonus_ratio, event.transfer_ratio)] = event
    return [events[key] for key in sorted(events)]


def fetch_dividend_events(ticker: str, timeout: float = 10) -> list[DividendEvent]:
    """请求单个标的历史分红送转；无数据（含 ETF）返回空列表。"""
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{str(ticker).strip()}")',
        "pageNumber": "1",
        "pageSize": "200",
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "1",
    }
    request = Request(
        f"{DIVIDEND_URL}?{urlencode(params)}",
        headers={
            "User-Agent": _UA,
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (URLError, OSError) as exc:
        raise DividendFetchError(f"分红数据请求失败：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DividendFetchError("分红数据返回内容不是合法 JSON") from exc
    return parse_eastmoney_dividends(payload, ticker)
