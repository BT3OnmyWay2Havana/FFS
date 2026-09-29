"""Live broker: real market orders through ccxt. Locked unless every live switch is set.

LIVE_DRY_RUN=true (the default) logs the order it would send and fills it on paper
prices instead, so you can watch the live path end to end before real money moves.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..store import Store
from .paper import Fill

log = logging.getLogger(__name__)


class LiveBroker:
    mode = "live"

    def __init__(self, store: Store, s: Settings):
        if not s.live_enabled:
            raise RuntimeError("Live trading is not fully enabled; refusing to create a live broker.")
        import ccxt.async_support as ccxt

        self.store = store
        self.s = s
        self.ex = getattr(ccxt, s.exchange_id)({
            "apiKey": s.exchange_api_key,
            "secret": s.exchange_api_secret,
            "enableRateLimit": True,
        })

    @property
    def cash(self) -> float:
        return float(self.store.get_meta("live_cash_snapshot", 0.0))

    async def refresh_cash(self) -> float:
        bal = await self.ex.fetch_balance()
        free = float((bal.get("free") or {}).get(self.s.quote) or 0.0)
        self.store.set_meta("live_cash_snapshot", free)
        return free

    async def _order(self, side: str, symbol: str, qty: float, price: float) -> Fill:
        value = qty * price
        if side == "buy" and value > self.s.live_max_order_value * 1.001:
            raise ValueError(f"order value {value:.2f} is over LIVE_MAX_ORDER_VALUE")
        await self.ex.load_markets()
        amount = float(self.ex.amount_to_precision(symbol, qty))
        if self.s.live_dry_run:
            log.warning("DRY RUN: would send market %s %s %s (~%.2f %s)", side, amount, symbol, value, self.s.quote)
            return Fill(price, amount, 0.0)
        order = await self.ex.create_order(symbol, "market", side, amount)
        filled = float(order.get("filled") or amount)
        avg = float(order.get("average") or order.get("price") or price)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)
        return Fill(avg, filled, fee)

    async def buy(self, symbol: str, qty: float, price: float) -> Fill:
        return await self._order("buy", symbol, qty, price)

    async def sell(self, symbol: str, qty: float, price: float) -> Fill:
        return await self._order("sell", symbol, qty, price)

    async def close(self) -> None:
        await self.ex.close()
