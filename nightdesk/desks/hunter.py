"""Hunter desk: looks for setups that fit fixed rules on hourly candles."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..indicators import atr, ema, rsi, sma
from ..market import Candle


@dataclass
class Setup:
    symbol: str
    kind: str  # "breakout" or "pullback"
    price: float
    stop: float
    target: float
    atr_pct: float
    rsi: float
    volume_ratio: float
    trend: str
    detail: str

    @property
    def reward_risk(self) -> float:
        risk = self.price - self.stop
        return (self.target - self.price) / risk if risk > 0 else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["reward_risk"] = round(self.reward_risk, 2)
        return d


BREAKOUT_LOOKBACK = 48  # bars (two days of hourly candles)
MIN_BARS = 120


def find_setup(symbol: str, candles: list[Candle]) -> Setup | None:
    """Return at most one long setup for the latest closed bar, or None."""
    if len(candles) < MIN_BARS:
        return None
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]

    price = closes[-1]
    e20, e50, e100 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 100)[-1]
    r = rsi(closes, 14)
    a = atr(highs, lows, closes, 14)
    if price <= 0 or a <= 0:
        return None
    atr_pct = a / price * 100
    avg_vol = sma(vols[:-1], 20)
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0.0
    trend = "up" if e20 > e50 > e100 else "down" if e20 < e50 < e100 else "mixed"

    prior_high = max(highs[-BREAKOUT_LOOKBACK - 1:-1])
    if price > prior_high and vol_ratio >= 1.5 and price > e50:
        stop = price - 1.5 * a
        return Setup(symbol, "breakout", price, stop, price + 3.0 * a, round(atr_pct, 2), round(r[-1], 1),
                     round(vol_ratio, 2), trend,
                     f"closed above the {BREAKOUT_LOOKBACK}h high {prior_high:.6g} on {vol_ratio:.1f}x volume")

    if trend == "up" and r[-2] < 40 <= r[-1] and price > e100:
        swing_low = min(lows[-6:])
        stop = min(swing_low, price - 1.2 * a)
        risk = price - stop
        return Setup(symbol, "pullback", price, stop, price + 2.5 * risk, round(atr_pct, 2), round(r[-1], 1),
                     round(vol_ratio, 2), trend,
                     f"RSI turned up through 40 ({r[-2]:.0f} to {r[-1]:.0f}) inside an uptrend")
    return None
