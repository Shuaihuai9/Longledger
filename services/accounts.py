"""账户管理服务：新建 / 删除 / 列举账户，以及全账户共享的交易参数。

界面层只调用本服务与 LedgerService，不直接触碰 SQL。
"""
from __future__ import annotations

from config.settings import LEDGER_MODE_SYSTEM, FeeConfig
from data.repositories import Repository
from services.ledger import LedgerService

MAX_NAME_LENGTH = 40


class AccountService:
    def __init__(self, conn) -> None:
        self.conn = conn
        # 账户管理与费率配置不依赖具体 account_id
        self.repo = Repository(conn)

    # ---------- 账户 ----------
    def list_accounts(self) -> list:
        return self.repo.list_accounts()

    def list_accounts_with_summary(self) -> list[dict]:
        rows = []
        for row in self.repo.list_accounts():
            account_id = int(row["account_id"])
            mode = str(row["ledger_mode"]) if "ledger_mode" in row.keys() else LEDGER_MODE_SYSTEM
            rows.append(
                {
                    "account_id": account_id,
                    "name": row["name"],
                    "initial_capital_cents": int(row["initial_capital_cents"]),
                    "cash_cents": self.repo.account_cash_cents(account_id),
                    "trade_count": self.repo.account_trade_count(account_id),
                    "ledger_mode": mode,
                }
            )
        return rows

    def create(
        self, name: str, initial_capital_cents: int, ledger_mode: str = LEDGER_MODE_SYSTEM
    ) -> int:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("账户名称不能为空")
        if len(clean) > MAX_NAME_LENGTH:
            raise ValueError(f"账户名称不能超过 {MAX_NAME_LENGTH} 个字符")
        if self.repo.account_name_exists(clean):
            raise ValueError(f"账户名称「{clean}」已存在")
        if int(initial_capital_cents) < 0:
            raise ValueError("初始本金不能为负")

        with self.repo.lock():
            try:
                account_id = self.repo.create_account(
                    clean, int(initial_capital_cents), ledger_mode
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        # 首次加载会自动补记期初入金流水，保证现金与初始本金一致
        LedgerService(self.conn, account_id)
        return account_id

    def delete(self, account_id: int) -> None:
        account_id = int(account_id)
        if self.repo.count_accounts() <= 1:
            raise ValueError("至少需要保留一个账户，无法删除最后一个账户")
        if self.repo.get_account(account_id) is None:
            raise ValueError(f"账户 {account_id} 不存在")
        with self.repo.lock():
            try:
                self.repo.delete_account(account_id)
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise

    # ---------- 全局交易参数 ----------
    def current_fee_config(self) -> FeeConfig:
        return self.repo.load_fee_config()

    def update_fee_config(self, config: FeeConfig) -> None:
        self.repo.save_fee_config(config)
