"""全局配置：费率、税率、数据路径与账户初始参数。

口径约定：
- 所有金额字段为 INTEGER，单位「分」。
- 所有价格字段为 INTEGER，单位「毫」（1 元 = 1000 毫）。
- 所有费率写为 Decimal 字符串，绝不使用 float。
"""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

APP_VERSION = "1.0.0"

# 安装包用 Inno Setup 写入 %PROGRAMDATA%\LongLedger\paths.ini：
#   [Paths]
#   AccountDataDir=C:\...
#   QuoteDataDir=D:\...
# 安装时由用户选择；未安装（源码运行）时回落到项目根目录，保持开发数据不动。
PATHS_INI = (
    Path(os.environ["PROGRAMDATA"]) / "LongLedger" / "paths.ini"
    if os.environ.get("PROGRAMDATA")
    else None
)


def _is_frozen() -> bool:
    # Nuitka 会注入模块级 __compiled__，不保证设置 sys.frozen
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _read_install_paths() -> tuple[str | None, str | None]:
    """读取安装器写入的两个数据目录；不存在或读不到时返回空。"""
    if PATHS_INI is None or not PATHS_INI.exists():
        return None, None
    # 安装器按 UTF-8 写入；兼容旧版工具留下的 ANSI(GBK) 文件
    for encoding in ("utf-8-sig", "gbk"):
        parser = configparser.ConfigParser()
        try:
            parser.read(PATHS_INI, encoding=encoding)
        except (OSError, configparser.Error, UnicodeDecodeError):
            continue
        if not parser.has_section("Paths"):
            return None, None
        account = parser.get("Paths", "AccountDataDir", fallback="").strip()
        quotes = parser.get("Paths", "QuoteDataDir", fallback="").strip()
        return account or None, quotes or None
    return None, None


def _default_dir(kind: str) -> Path:
    """账户数据默认 %APPDATA%，行情数据默认 %LOCALAPPDATA%；开发态用项目根目录。"""
    if not _is_frozen():
        return PROJECT_ROOT
    if kind == "account":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "LongLedger" / "data"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "LongLedger" / "market"


_install_account, _install_quotes = _read_install_paths()

ACCOUNT_DATA_DIR = Path(_install_account) if _install_account else _default_dir("account")
QUOTE_DATA_DIR = Path(_install_quotes) if _install_quotes else _default_dir("quotes")

DB_PATH = ACCOUNT_DATA_DIR / "longledger.db"
QUOTES_DB_PATH = QUOTE_DATA_DIR / "market.db"

# 默认初始入金 10 万元
DEFAULT_INITIAL_CAPITAL_CENTS = 10_000_000

# 净值缩放倍数：净值统一以整数存储，避免浮点
NAV_SCALE = 1_000_000

# 记账口径：SYSTEM = 费率引擎计算费用与红利；STATEMENT = 一律以导入交割单的真实金额为准
LEDGER_MODE_SYSTEM = "SYSTEM"
LEDGER_MODE_STATEMENT = "STATEMENT"
LEDGER_MODES = (LEDGER_MODE_SYSTEM, LEDGER_MODE_STATEMENT)
LEDGER_MODE_LABELS = {
    LEDGER_MODE_SYSTEM: "系统计算口径",
    LEDGER_MODE_STATEMENT: "交割单口径",
}


@dataclass(frozen=True)
class FeeConfig:
    """交易摩擦成本配置。"""

    commission_rate: Decimal = Decimal("0.00025")   # 佣金万 2.5
    min_commission_cents: int = 500                 # 最低 5 元保底
    stamp_tax_rate: Decimal = Decimal("0.0005")     # 印花税千 0.5，仅卖出单边
    transfer_fee_rate: Decimal = Decimal("0.00001")  # 过户费十万分之一，双边
    etf_stamp_tax_exempt: bool = True               # ETF 卖出免印花税
    etf_transfer_fee_exempt: bool = True            # ETF 免过户费


@dataclass(frozen=True)
class DividendTaxConfig:
    """红利税差异化税率（卖出时按持股期限补扣）。"""

    rate_within_one_month: Decimal = Decimal("0.20")
    rate_within_one_year: Decimal = Decimal("0.10")
    rate_over_one_year: Decimal = Decimal("0")
    etf_exempt: bool = True                          # ETF 现金分红免红利税


DEFAULT_FEE_CONFIG = FeeConfig()
DEFAULT_DIVIDEND_TAX_CONFIG = DividendTaxConfig()
