"""移动加权平均成本池：券商口径的「已实现盈亏」。

批次账本（FIFO）继续负责红利税档位与持仓明细；本模块只回答一个问题：
某笔卖出按「加权摊薄成本」赚了多少，口径与券商 APP 保持一致：

- 买入：实际现金流出（成交额 + 费用；交割单口径为发生金额）全部计入成本
- 现金分红：从总成本中扣除，摊薄持股成本；股息红利税（差异扣税）是分红的追回，
  反过来加回成本
- 送转 / 红股 / 新股入账：只增加股数，成本不变（单位成本自然摊薄）
- 卖出：结转成本 = 总成本 × 卖出数量 ÷ 总股数；清仓时剩余成本全部结转，保证闭合
- 已实现盈亏 = 卖出净额（已扣佣金、印花税、过户费）− 结转成本

因此分红越多、持有越久，卖出成本越低，已实现盈亏与券商 APP 的「已实现盈亏」一致。
"""
from __future__ import annotations

from decimal import Decimal

from core.money import round_half_up


class AverageCostPool:
    """单账户、按标的聚合的移动加权平均成本池，单位：股 / 分。"""

    def __init__(self) -> None:
        self._qty: dict[str, int] = {}
        self._cost: dict[str, int] = {}

    def qty(self, symbol: str) -> int:
        return self._qty.get(symbol, 0)

    def cost_cents(self, symbol: str) -> int:
        return self._cost.get(symbol, 0)

    def buy(self, symbol: str, qty: int, cash_cents: int) -> None:
        """买入：股数与实际现金流出（含费用）一起并入成本池。"""
        if qty <= 0:
            return
        self._qty[symbol] = self.qty(symbol) + qty
        self._cost[symbol] = self.cost_cents(symbol) + max(0, int(cash_cents))

    def add_shares(self, symbol: str, qty: int) -> None:
        """送转 / 红股 / 新股入账：只加股数，不加成本。"""
        if qty > 0:
            self._qty[symbol] = self.qty(symbol) + qty

    def reduce_cost(self, symbol: str, cents: int) -> None:
        """分红抵扣成本（分）；传负数表示股息红利税，等价于把红利吐回去。

        只在仍有持股时生效：已清仓的红利税属于已实现损失，不再挂到未来成本上。
        """
        if self.qty(symbol) <= 0:
            return
        self._cost[symbol] = self.cost_cents(symbol) - int(cents)

    def release(self, symbol: str, qty: int) -> int:
        """卖出结转成本（分）；清仓时把剩余成本全部结转，避免尾差残留。"""
        shares = self.qty(symbol)
        cost = self.cost_cents(symbol)
        if qty <= 0 or shares <= 0:
            return 0
        if qty >= shares:
            self._qty[symbol] = 0
            self._cost[symbol] = 0
            return cost
        released = round_half_up(Decimal(cost) * qty / shares)
        self._qty[symbol] = shares - qty
        self._cost[symbol] = cost - released
        return released
