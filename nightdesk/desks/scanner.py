"""Scanner desk: ranks every pair by 24h move and traded value, and keeps the watch list."""

from __future__ import annotations

from dataclasses import dataclass

from ..market import Ticker


@dataclass
class Mover:
    symbol: str
    last: float
    change_pct: float
    quote_volume: float
    score: float


def rank_movers(tickers: dict[str, Ticker], min_quote_volume: float, top_n: int) -> list[Mover]:
    """Score = rank by absolute 24h move + rank by traded value. Illiquid pairs are dropped."""
    liquid = [t for t in tickers.values() if t.quote_volume >= min_quote_volume]
    if not liquid:
        return []
    by_move = sorted(liquid, key=lambda t: abs(t.change_pct), reverse=True)
    by_vol = sorted(liquid, key=lambda t: t.quote_volume, reverse=True)
    n = len(liquid)
    move_rank = {t.symbol: n - i for i, t in enumerate(by_move)}
    vol_rank = {t.symbol: n - i for i, t in enumerate(by_vol)}
    movers = [
        Mover(t.symbol, t.last, t.change_pct, t.quote_volume,
              round((move_rank[t.symbol] + vol_rank[t.symbol]) / (2 * n), 3))
        for t in liquid
    ]
    movers.sort(key=lambda m: m.score, reverse=True)
    return movers[:top_n]


def watch_symbols(movers: list[Mover], watchlist: list[str], quote: str, available: set[str]) -> list[str]:
    """Fixed watch list first, then today's movers, only for pairs the exchange lists."""
    ordered: list[str] = []
    for base in watchlist:
        sym = f"{base}/{quote}"
        if sym in available and sym not in ordered:
            ordered.append(sym)
    for m in movers:
        if m.symbol not in ordered:
            ordered.append(m.symbol)
    return ordered
