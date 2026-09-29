"""Small, dependency-free indicator maths on plain lists."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window) if window else 0.0


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns a list aligned with closes (first `period` values are 50)."""
    if len(closes) <= period:
        return [50.0] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [50.0] * (period + 1)

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[-1] = value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(value(avg_gain, avg_loss))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Average true range over the last `period` bars."""
    if len(closes) < 2:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    window = trs[-period:]
    return sum(window) / len(window)
