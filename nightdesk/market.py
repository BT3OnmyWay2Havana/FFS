"""Market data. `ExchangeMarket` wraps ccxt; `SimMarket` is an offline random walk for demo and tests."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Ticker:
    symbol: str
    last: float
    change_pct: float  # 24h
    quote_volume: float  # 24h, in quote currency


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    id: str
    ts: int
    side: str  # "buy" or "sell" (taker side)
    price: float
    amount: float

    @property
    def value(self) -> float:
        return self.price * self.amount


class Market(Protocol):
    async def tickers(self) -> dict[str, Ticker]: ...
    async def candles(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[Candle]: ...
    async def trades(self, symbol: str, limit: int = 500) -> list[Trade]: ...
    async def last_price(self, symbol: str) -> float: ...
    async def close(self) -> None: ...


class ExchangeMarket:
    """Public market data through ccxt's unified API. No keys needed for reads."""

    def __init__(self, exchange_id: str, quote: str):
        import ccxt.async_support as ccxt

        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Unknown exchange '{exchange_id}' for ccxt.")
        self.ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        self.quote = quote
        self._symbols: list[str] | None = None

    async def _spot_symbols(self) -> list[str]:
        if self._symbols is None:
            markets = await self.ex.load_markets()
            self._symbols = [
                s for s, m in markets.items()
                if m.get("spot") and m.get("active", True) is not False and m.get("quote") == self.quote
            ]
        return self._symbols

    async def tickers(self) -> dict[str, Ticker]:
        symbols = await self._spot_symbols()
        raw = await self.ex.fetch_tickers(symbols)
        out: dict[str, Ticker] = {}
        for sym, t in raw.items():
            last = t.get("last") or t.get("close")
            if not last:
                continue
            qv = t.get("quoteVolume")
            if qv is None and t.get("baseVolume") is not None:
                qv = t["baseVolume"] * (t.get("vwap") or last)
            out[sym] = Ticker(sym, float(last), float(t.get("percentage") or 0.0), float(qv or 0.0))
        return out

    async def candles(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[Candle]:
        rows = await self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)) for r in rows]

    async def trades(self, symbol: str, limit: int = 500) -> list[Trade]:
        rows = await self.ex.fetch_trades(symbol, limit=limit)
        return [
            Trade(str(r.get("id") or f"{r['timestamp']}-{i}"), int(r["timestamp"]), r.get("side") or "buy",
                  float(r["price"]), float(r["amount"]))
            for i, r in enumerate(rows)
        ]

    async def last_price(self, symbol: str) -> float:
        t = await self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close"))

    async def close(self) -> None:
        await self.ex.close()


class SimMarket:
    """Deterministic-ish random walk so the whole desk can run with no network or keys."""

    BASE = {"BTC": 64000, "ETH": 3100, "SOL": 145, "XRP": 0.55, "ADA": 0.42, "DOGE": 0.12,
            "LINK": 14.0, "AVAX": 28.0, "DOT": 6.2, "LTC": 72.0}

    def __init__(self, quote: str = "USD", seed: int = 7):
        self.quote = quote
        self.rng = random.Random(seed)
        self.hist: dict[str, list[Candle]] = {}
        now = int(time.time() // 3600 * 3600 * 1000)
        for base, price in self.BASE.items():
            sym = f"{base}/{quote}"
            candles, p = [], float(price)
            drift = self.rng.uniform(-0.0006, 0.0012)
            for i in range(240):
                o = p
                p = max(p * (1 + drift + self.rng.gauss(0, 0.009)), 1e-6)
                hi, lo = max(o, p) * (1 + abs(self.rng.gauss(0, 0.003))), min(o, p) * (1 - abs(self.rng.gauss(0, 0.003)))
                candles.append(Candle(now - (240 - i) * 3_600_000, o, hi, lo, p, self.rng.uniform(50, 150) * 1000 / math.sqrt(price)))
            self.hist[sym] = candles
        self._plant_breakout(f"SOL/{quote}")
        self._last_step = time.time()

    def _plant_breakout(self, sym: str) -> None:
        """Give the demo one clean breakout so the whole approval flow can be seen."""
        candles = self.hist[sym]
        p = candles[-60].close
        for c in candles[-59:-1]:
            o, p = p, p * (1 + 0.0012 + self.rng.gauss(0, 0.002))
            c.open, c.close, c.high, c.low = o, p, max(o, p) * 1.001, min(o, p) * 0.999
        last, prior_high = candles[-1], max(c.high for c in candles[-49:-1])
        last.open, last.close = candles[-2].close, prior_high * 1.012
        last.high, last.low = last.close * 1.001, last.open * 0.999
        last.volume = sum(c.volume for c in candles[-21:-1]) / 20 * 3

    def _step(self) -> None:
        """Advance prices a little on every read, so the demo feels alive."""
        now = time.time()
        steps = min(int((now - self._last_step) / 5), 10)
        if steps <= 0:
            return
        self._last_step = now
        for sym, candles in self.hist.items():
            c = candles[-1]
            for _ in range(steps):
                p = max(c.close * (1 + self.rng.gauss(0.00002, 0.0005)), 1e-6)
                c.high, c.low, c.close = max(c.high, p), min(c.low, p), p
                c.volume += self.rng.uniform(0, 5000) / math.sqrt(max(p, 1e-6))

    async def tickers(self) -> dict[str, Ticker]:
        self._step()
        out = {}
        for sym, candles in self.hist.items():
            last, prev = candles[-1].close, candles[-25].close
            qv = sum(c.volume * c.close for c in candles[-24:])
            out[sym] = Ticker(sym, last, (last / prev - 1) * 100, qv)
        return out

    async def candles(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[Candle]:
        self._step()
        return [Candle(**vars(c)) for c in self.hist[symbol][-limit:]]

    async def trades(self, symbol: str, limit: int = 500) -> list[Trade]:
        self._step()
        last = self.hist[symbol][-1].close
        now = int(time.time() * 1000)
        out = []
        for i in range(limit):
            size = self.rng.lognormvariate(0, 1.4) * 2000 / last
            out.append(Trade(f"{symbol}-{now // 60000}-{i}", now - i * 500, self.rng.choice(["buy", "sell"]),
                             last * (1 + self.rng.gauss(0, 0.0005)), size))
        return out

    async def last_price(self, symbol: str) -> float:
        self._step()
        return self.hist[symbol][-1].close

    async def close(self) -> None:
        return None
