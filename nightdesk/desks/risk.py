"""Risk desk: every idea is checked against fixed limits. It can only say no or shrink a size."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings


@dataclass
class Idea:
    symbol: str
    entry: float
    stop: float
    target: float
    atr_pct: float


@dataclass
class Book:
    equity: float
    cash: float
    day_start_equity: float
    open_symbols: list[str]
    last_proposal_ts: dict[str, float] = field(default_factory=dict)


@dataclass
class Verdict:
    ok: bool
    qty: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "within limits"


def size_position(idea: Idea, book: Book, s: Settings, live: bool) -> float:
    """Size so a stop-out loses risk_per_trade_pct of equity, capped by position and cash limits."""
    per_unit_risk = idea.entry - idea.stop
    if per_unit_risk <= 0 or idea.entry <= 0:
        return 0.0
    by_risk = book.equity * s.risk_per_trade_pct / 100 / per_unit_risk
    by_cap = book.equity * s.max_position_pct / 100 / idea.entry
    by_cash = book.cash * 0.98 / idea.entry
    qty = min(by_risk, by_cap, by_cash)
    if live:
        qty = min(qty, s.live_max_order_value / idea.entry)
    return max(qty, 0.0)


def check(idea: Idea, book: Book, s: Settings, live: bool = False, now: float | None = None,
          check_cooldown: bool = True) -> Verdict:
    now = now or time.time()
    reasons: list[str] = []

    if idea.stop >= idea.entry:
        reasons.append("stop is not below entry")
    risk = idea.entry - idea.stop
    rr = (idea.target - idea.entry) / risk if risk > 0 else 0.0
    if rr < s.min_reward_risk:
        reasons.append(f"reward/risk {rr:.2f} below {s.min_reward_risk}")
    if idea.atr_pct > s.max_atr_pct:
        reasons.append(f"too volatile (ATR {idea.atr_pct:.1f}% > {s.max_atr_pct}%)")
    if idea.symbol in book.open_symbols:
        reasons.append("already holding it")
    if len(book.open_symbols) >= s.max_open_positions:
        reasons.append(f"book full ({s.max_open_positions} positions)")
    day_loss_pct = (book.day_start_equity - book.equity) / book.day_start_equity * 100 if book.day_start_equity else 0
    if day_loss_pct >= s.max_daily_loss_pct:
        reasons.append(f"daily loss limit hit ({day_loss_pct:.1f}%)")
    if check_cooldown:
        last = book.last_proposal_ts.get(idea.symbol)
        if last and now - last < s.symbol_cooldown_min * 60:
            reasons.append("proposed recently (cooldown)")

    qty = size_position(idea, book, s, live) if not reasons else 0.0
    if not reasons and qty * idea.entry < s.min_order_value:
        reasons.append(f"size too small (under {s.min_order_value:g} {s.quote})")
    return Verdict(ok=not reasons, qty=qty if not reasons else 0.0, reasons=reasons)


def drift_ok(proposed_entry: float, current: float, s: Settings) -> tuple[bool, float]:
    """At approval time, refuse if price has run away from the proposed entry."""
    drift = (current / proposed_entry - 1) * 100
    return abs(drift) <= s.max_price_drift_pct, drift
