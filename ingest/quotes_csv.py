"""日线行情 CSV 导入。

支持两种表头：
- 英文：date,symbol,close
- 中文：日期,代码,收盘价
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from core.money import yuan_to_mills
from data.repositories import Repository

_ALIASES = {
    "date": "date",
    "日期": "date",
    "symbol": "symbol",
    "代码": "symbol",
    "code": "symbol",
    "close": "close",
    "收盘价": "close",
    "close_price": "close",
}


def _normalize(row: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        canonical = _ALIASES.get(key.strip())
        if canonical:
            result[canonical] = (value or "").strip()
    return result


def import_quotes_csv(repo: Repository, csv_path: str | Path) -> int:
    """导入行情，返回写入行数。价格按毫级整数入库。"""
    count = 0
    path = Path(csv_path)
    with repo.lock():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = _normalize(raw)
                if not row.get("date") or not row.get("symbol") or not row.get("close"):
                    continue
                price_date = date.fromisoformat(row["date"])
                close_mills = yuan_to_mills(Decimal(row["close"]))
                if close_mills <= 0:
                    raise ValueError(f"{row['symbol']} {row['date']} 收盘价必须为正")
                repo.upsert_quote(row["symbol"], price_date, close_mills)
                count += 1
        repo.commit()
    return count
