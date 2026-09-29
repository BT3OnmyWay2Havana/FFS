"""News and sentiment desk, backed by LunarCrush API v4 (optional, needs a paid plan for social data).

Field names follow the published v4 reference for /public/coins/list/v2.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

BASE_URL = "https://lunarcrush.com/api4"


@dataclass
class Sentiment:
    symbol: str
    galaxy_score: float | None
    galaxy_score_previous: float | None
    alt_rank: int | None
    alt_rank_previous: int | None
    sentiment: float | None
    social_volume_24h: float | None
    interactions_24h: float | None
    social_dominance: float | None

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def parse_coins(payload, wanted: set[str]) -> dict[str, Sentiment]:
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    out: dict[str, Sentiment] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol", "")).upper()
        if sym not in wanted:
            continue
        out[sym] = Sentiment(
            sym,
            _num(r.get("galaxy_score")), _num(r.get("galaxy_score_previous")),
            _int(r.get("alt_rank")), _int(r.get("alt_rank_previous")),
            _num(r.get("sentiment")), _num(r.get("social_volume_24h")),
            _num(r.get("interactions_24h")), _num(r.get("social_dominance")),
        )
    return out


async def fetch_sentiment(api_key: str, wanted: set[str], client: httpx.AsyncClient | None = None) -> dict[str, Sentiment]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(
            f"{BASE_URL}/public/coins/list/v2",
            params={"sort": "market_cap_rank", "limit": 200},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return parse_coins(resp.json(), wanted)
    finally:
        if own:
            await client.aclose()


def headline(s: Sentiment) -> str | None:
    """One line for the squawk feed when something moved, else None."""
    parts = []
    if s.galaxy_score is not None and s.galaxy_score_previous is not None:
        diff = s.galaxy_score - s.galaxy_score_previous
        if abs(diff) >= 5:
            parts.append(f"galaxy {s.galaxy_score:.0f} ({diff:+.0f})")
    if s.alt_rank is not None and s.alt_rank_previous is not None:
        jump = s.alt_rank_previous - s.alt_rank
        if abs(jump) >= 50:
            parts.append(f"altrank {s.alt_rank} ({jump:+d} places)")
    if s.sentiment is not None and (s.sentiment >= 80 or s.sentiment <= 40):
        parts.append(f"sentiment {s.sentiment:.0f}")
    return " · ".join(parts) if parts else None
