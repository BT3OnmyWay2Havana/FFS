"""Paper broker: simulated fills against live prices, with fees and slippage."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..store import Store


@dataclass
class Fill:
    price: float
    qty: float
    fee: float


class PaperBroker:
    mode = "paper"

    def __init__(self, store: Store, s: Settings):
        self.store = store
        self.s = s
        if store.get_meta("cash") is None:
            store.set_meta("cash", s.start_equity)

    @property
    def cash(self) -> float:
        return float(self.store.get_meta("cash", self.s.start_equity))

    async def buy(self, symbol: str, qty: float, price: float) -> Fill:
        fill_price = price * (1 + self.s.paper_slippage_pct / 100)
        cost = fill_price * qty
        fee = cost * self.s.paper_fee_pct / 100
        if cost + fee > self.cash:
            raise ValueError("not enough paper cash")
        self.store.set_meta("cash", self.cash - cost - fee)
        return Fill(fill_price, qty, fee)

    async def sell(self, symbol: str, qty: float, price: float) -> Fill:
        fill_price = price * (1 - self.s.paper_slippage_pct / 100)
        proceeds = fill_price * qty
        fee = proceeds * self.s.paper_fee_pct / 100
        self.store.set_meta("cash", self.cash + proceeds - fee)
        return Fill(fill_price, qty, fee)
