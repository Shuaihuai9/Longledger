"""分红送转公告 CSV 导入。

表头：symbol,ex_date,cash_per_share,bonus_ratio,transfer_ratio,pay_date
其中 cash_per_share 为「每股」现金红利（元），bonus_ratio/transfer_ratio 为「每股」比例，
例如 10 派 5 元 记 0.50，10 送 3 记 0.3，10 转 2 记 0.2。
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from core.corporate_actions import CorporateAction
from core.money import yuan_to_mills

_ALIASES = {
    "symbol": "symbol", "代码": "symbol",
    "ex_date": "ex_date", "除权除息日": "ex_date", "除息日": "ex_date",
    "cash_per_share": "cash", "每股红利": "cash", "每股派现": "cash",
    "bonus_ratio": "bonus", "每股送股": "bonus",
    "transfer_ratio": "transfer", "每股转增": "transfer",
    "pay_date": "pay_date", "派息日": "pay_date",
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


def load_actions_csv(csv_path: str | Path) -> list[CorporateAction]:
    """解析公告为待执行的公司行为列表，由服务层按前收盘价执行。"""
    actions: list[CorporateAction] = []
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            row = _normalize(raw)
            if not row.get("symbol") or not row.get("ex_date"):
                continue
            actions.append(
                CorporateAction(
                    symbol=row["symbol"],
                    ex_date=date.fromisoformat(row["ex_date"]),
                    cash_per_share_mills=yuan_to_mills(Decimal(row.get("cash") or "0")),
                    bonus_ratio=Decimal(row.get("bonus") or "0"),
                    transfer_ratio=Decimal(row.get("transfer") or "0"),
                    pay_date=date.fromisoformat(row["pay_date"]) if row.get("pay_date") else None,
                )
            )
    return actions
