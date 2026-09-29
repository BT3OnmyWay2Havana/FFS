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

    @property
    def net_value(self) -> float:
        return self.big_buy_value - self.big_sell_value

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
