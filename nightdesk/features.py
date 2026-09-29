"""Turns raw candles into the facts the specialist analysts read. Same shape live and in the test suite."""

from __future__ import annotations

from typing import Any

from .indicators import atr, ema, rsi
from .market import Candle


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def pivots(values: list[float], lows: bool, span: int = 2, keep: int = 3) -> list[float]:
    """Most recent swing lows (or highs): a bar lower (higher) than `span` bars either side."""
    out: list[float] = []
    for i in range(len(values) - span - 1, span - 1, -1):
        window = values[i - span:i + span + 1]
        v = values[i]
        if (lows and v == min(window)) or (not lows and v == max(window)):
            out.append(v)
            if len(out) == keep:
                break
    return out


def trend_label(e_fast: float, e_mid: float, e_slow: float | None = None) -> str:
    if e_slow is None:
        return "up" if e_fast > e_mid else "down" if e_fast < e_mid else "flat"
    if e_fast > e_mid > e_slow:
        return "up"
    if e_fast < e_mid < e_slow:
        return "down"
    return "mixed"


def hourly(candles: list[Candle]) -> dict[str, Any]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    price = closes[-1]
    e20, e50, e100 = ema(closes, 20), ema(closes, 50), ema(closes, 100)
    r = rsi(closes, 14)
    a = atr(highs, lows, closes, 14)
    atr_hist = [atr(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14) / closes[i] * 100
                for i in range(len(closes) - 50, len(closes))] if len(closes) > 70 else [a / price * 100]
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else (sum(vols) / len(vols))
    last12 = []
    for c in candles[-12:]:
        last12.append([_r(c.open, 6), _r(c.high, 6), _r(c.low, 6), _r(c.close, 6),
                       _r(c.volume / avg_vol if avg_vol else 0, 2)])
    return {
        "price": _r(price, 6),
        "ema20": _r(e20[-1], 6), "ema50": _r(e50[-1], 6), "ema100": _r(e100[-1], 6),
        "trend": trend_label(e20[-1], e50[-1], e100[-1]),
        "dist_from_ema20_atr": _r((price - e20[-1]) / a if a else 0, 2),
        "rsi14": _r(r[-1], 1), "rsi14_prev": _r(r[-2], 1),
        "atr": _r(a, 6), "atr_pct": _r(a / price * 100, 2),
        "atr_pct_avg_50h": _r(sum(atr_hist) / len(atr_hist), 2),
        "volume_ratio": _r(vols[-1] / avg_vol if avg_vol else 0, 2),
        "high_48h_prior": _r(max(highs[-49:-1]), 6), "low_48h_prior": _r(min(lows[-49:-1]), 6),
        "change_24h_pct": _r(_pct(price, closes[-25]), 2) if len(closes) > 25 else None,
        "change_7d_pct": _r(_pct(price, closes[-169]), 2) if len(closes) > 169 else None,
        "swing_lows": [_r(v, 6) for v in pivots(lows, True)],
        "swing_highs": [_r(v, 6) for v in pivots(highs, False)],
        "last_12_bars_ohlc_volratio": last12,
    }


def daily(candles: list[Candle]) -> dict[str, Any]:
    closes = [c.close for c in candles]
    if len(closes) < 30:
        return {"available": False}
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    price = closes[-1]
    return {
        "available": True,
        "close": _r(price, 6), "ema20": _r(e20, 6), "ema50": _r(e50, 6),
        "trend": trend_label(e20, e50),
        "price_vs_ema50_pct": _r(_pct(price, e50), 2),
        "high_30d": _r(max(c.high for c in candles[-30:]), 6),
        "low_30d": _r(min(c.low for c in candles[-30:]), 6),
        "dist_to_30d_high_pct": _r(_pct(max(c.high for c in candles[-30:]), price), 2),
        "change_30d_pct": _r(_pct(price, closes[-30]), 2),
    }


def btc_context(h: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    return {
        "change_24h_pct": h.get("change_24h_pct"),
        "trend_1h": h.get("trend"),
        "rsi14_1h": h.get("rsi14"),
        "trend_1d": d.get("trend") if d.get("available") else None,
        "price_vs_daily_ema50_pct": d.get("price_vs_ema50_pct") if d.get("available") else None,
    }


def case(symbol: str, setup: dict[str, Any], h: dict[str, Any], d: dict[str, Any],
         flow: dict[str, Any] | None, sentiment: dict[str, Any] | None, quote_volume_24h: float | None) -> dict[str, Any]:
    """Everything the panel knows about one setup."""
    return {
        "symbol": symbol,
        "setup": {k: setup[k] for k in ("kind", "price", "stop", "target", "reward_risk", "detail")},
        "hourly": h,
        "daily": d,
        "flow": flow,
        "sentiment": sentiment,
        "quote_volume_24h": round(quote_volume_24h) if quote_volume_24h else None,
    }


def round_data(time_utc: str, weekday: str, quote: str, btc: dict[str, Any], book: dict[str, Any],
               limits: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """One review round: shared context plus the cases. Same shape live and in the test suite."""
    return {
        "time_utc": time_utc,
        "weekday": weekday,
        "quote_currency": quote,
        "btc_context": btc,
        "book": book,
        "limits": limits,
        "setups": cases,
    }
