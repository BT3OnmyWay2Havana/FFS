"""Whale desk: flags unusually large trades on the exchange tape.

It cannot see on-chain wallets. It watches the exchange's public trade feed and
treats a trade as "large" when it is above both a fixed floor and the 99th
percentile of recent trade sizes for that pair.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..market import Trade


@dataclass
class WhaleFlow:
    symbol: str
    big_buys: int
    big_sells: int
    big_buy_value: float
    big_sell_value: float
    threshold: float
    window_minutes: float = 0.0
    price_change_pct: float = 0.0  # first to last trade in the sample
    total_value: float = 0.0  # all trades in the sample

    @property
    def net_value(self) -> float:
        return self.big_buy_value - self.big_sell_value

    def facts(self, quote_volume_24h: float | None = None) -> dict:
        """What the order-flow analyst reads."""
        big = self.big_buy_value + self.big_sell_value
        return {
            "bias": self.bias,
            "big_buys": self.big_buys, "big_sells": self.big_sells,
            "big_buy_value": round(self.big_buy_value), "big_sell_value": round(self.big_sell_value),
            "net_value": round(self.net_value),
            "large_trade_threshold": round(self.threshold),
            "large_share_of_sample_pct": round(big / self.total_value * 100, 1) if self.total_value else 0.0,
            "large_value_vs_24h_volume_pct": round(big / quote_volume_24h * 100, 3) if quote_volume_24h else None,
            "sample_window_minutes": round(self.window_minutes, 1),
            "price_change_over_window_pct": round(self.price_change_pct, 3),
        }

    @property
    def bias(self) -> str:
        total = self.big_buy_value + self.big_sell_value
        if total == 0:
            return "quiet"
        share = self.big_buy_value / total
        return "buying" if share >= 0.65 else "selling" if share <= 0.35 else "two-way"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def analyse(symbol: str, trades: list[Trade], min_value: float, seen: set[str]) -> tuple[WhaleFlow, list[Trade]]:
    """Summarise large prints; return the flow and any large trades not seen before."""
    values = [t.value for t in trades]
    threshold = max(min_value, percentile(values, 0.99))
    flow = WhaleFlow(symbol, 0, 0, 0.0, 0.0, threshold)
    if trades:
        ordered = sorted(trades, key=lambda t: t.ts)
        flow.window_minutes = (ordered[-1].ts - ordered[0].ts) / 60_000
        flow.price_change_pct = (ordered[-1].price / ordered[0].price - 1) * 100 if ordered[0].price else 0.0
        flow.total_value = sum(values)
    fresh: list[Trade] = []
    for t in trades:
        if t.value < threshold:
            continue
        if t.side == "buy":
            flow.big_buys += 1
            flow.big_buy_value += t.value
        else:
            flow.big_sells += 1
            flow.big_sell_value += t.value
        if t.id not in seen:
            seen.add(t.id)
            fresh.append(t)
    return flow, fresh
