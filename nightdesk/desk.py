"""The trading floor: runs every desk on its own schedule and routes notes to the chief and to you."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .desks import hunter, news, risk, scanner, whale
from .desks.chief import Chief, ChiefResult
from .market import Market
from .store import Store
from .telegram import Telegram, esc

log = logging.getLogger(__name__)

SETUP_MAX_AGE = 2 * 3600
WHALE_MIN_VALUE = 100_000  # quote currency; floor for a trade to count as "large"


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")


def fmt_price(p: float) -> str:
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.6f}"


class Desk:
    def __init__(self, s: Settings, store: Store, market: Market, chief: Chief, broker: Any, tg: Telegram):
        self.s, self.store, self.market, self.chief, self.broker, self.tg = s, store, market, chief, broker, tg
        self.movers: list[scanner.Mover] = []
        self.watch: list[str] = []
        self.prices: dict[str, float] = {}
        self.setups: dict[str, tuple[hunter.Setup, float]] = {}
        self.flows: dict[str, whale.WhaleFlow] = {}
        self.sentiment: dict[str, news.Sentiment] = {}
        self.whale_seen: set[str] = set()
        self.reviewed: dict[str, tuple[str, float]] = {}  # symbol -> (setup kind, when the chief saw it)
        self.last_run: dict[str, float] = {}
        self._decide_lock = asyncio.Lock()

    # -- state helpers -------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(self.store.get_meta("paused", False))

    def equity(self) -> float:
        held = sum(p["qty"] * self.prices.get(p["symbol"], p["entry"]) for p in self.store.open_positions())
        return self.broker.cash + held

    def day_start_equity(self) -> float:
        key = f"day_start:{utc_day()}"
        val = self.store.get_meta(key)
        if val is None:
            val = self.equity()
            self.store.set_meta(key, val)
        return float(val)

    def book(self) -> risk.Book:
        opens = self.store.open_positions()
        last = {}
        for sym in {p["symbol"] for p in self.store.recent_proposals(200)}:
            ts = self.store.last_proposal_ts(sym)
            if ts:
                last[sym] = ts
        return risk.Book(self.equity(), self.broker.cash, self.day_start_equity(),
                         [p["symbol"] for p in opens], last)

    def claude_calls_today(self) -> int:
        return int(self.store.get_meta(f"claude_calls:{utc_day()}", 0))

    # -- desks ---------------------------------------------------------------
    async def scan_and_hunt(self) -> None:
        tickers = await self.market.tickers()
        self.prices.update({k: t.last for k, t in tickers.items()})
        self.movers = scanner.rank_movers(tickers, self.s.min_quote_volume, self.s.scan_top_n)
        self.watch = scanner.watch_symbols(self.movers, self.s.watchlist, self.s.quote, set(tickers))
        if self.movers:
            top = self.movers[0]
            self.store.note("SCAN", f"{len(tickers)} pairs · top mover {top.change_pct:+.1f}% on the day", top.symbol)
        self.last_run["SCAN"] = time.time()

        found = 0
        for sym in self.watch:
            try:
                setup = hunter.find_setup(sym, await self.market.candles(sym, "1h", 200))
            except Exception as e:
                log.warning("hunter %s: %s", sym, e)
                continue
            if setup:
                prev = self.setups.get(sym)
                self.setups[sym] = (setup, time.time())
                if not prev or prev[0].kind != setup.kind or time.time() - prev[1] > SETUP_MAX_AGE:
                    self.store.note("HUNT", f"{setup.kind} · {setup.detail} · R/R {setup.reward_risk:.1f}", sym)
                found += 1
        self.setups = {k: v for k, v in self.setups.items() if time.time() - v[1] <= SETUP_MAX_AGE}
        if not found:
            self.store.note("HUNT", f"checked {len(self.watch)} charts · nothing fits the rules")
        self.last_run["HUNT"] = time.time()

    async def watch_whales(self) -> None:
        for sym in self.watch[:12]:
            try:
                trades = await self.market.trades(sym, 500)
            except Exception as e:
                log.warning("whale %s: %s", sym, e)
                continue
            flow, fresh = whale.analyse(sym, trades, WHALE_MIN_VALUE, self.whale_seen)
            self.flows[sym] = flow
            if fresh:
                val = sum(t.value for t in fresh)
                buys = sum(1 for t in fresh if t.side == "buy")
                self.store.note("WHALE", f"{len(fresh)} large prints ({buys} buys) · {val:,.0f} {self.s.quote} · {flow.bias}", sym)
        if len(self.whale_seen) > 50_000:
            self.whale_seen.clear()
        self.last_run["WHALE"] = time.time()

    async def read_news(self) -> None:
        if not self.s.lunarcrush_api_key:
            return
        wanted = {sym.split("/")[0] for sym in self.watch} | set(self.s.watchlist)
        try:
            self.sentiment = await news.fetch_sentiment(self.s.lunarcrush_api_key, wanted)
        except Exception as e:
            self.store.note("NEWS", f"LunarCrush unavailable: {e}", level="warn")
            return
        for base, snt in self.sentiment.items():
            line = news.headline(snt)
            if line:
                self.store.note("NEWS", line, f"{base}/{self.s.quote}")
        self.last_run["NEWS"] = time.time()

    def _chief_input(self, candidates: list[hunter.Setup]) -> dict[str, Any]:
        book = self.book()
        return {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "quote_currency": self.s.quote,
            "setups": [c.as_dict() for c in candidates],
            "whale_flow": [
                {"symbol": f.symbol, "bias": f.bias, "big_buys": f.big_buys, "big_sells": f.big_sells,
                 "net_value": round(f.net_value), "large_trade_threshold": round(f.threshold)}
                for sym, f in self.flows.items() if sym in {c.symbol for c in candidates}
            ],
            "sentiment": [
                self.sentiment[c.symbol.split("/")[0]].as_dict()
                for c in candidates if c.symbol.split("/")[0] in self.sentiment
            ],
            "market_movers": [{"symbol": m.symbol, "change_24h_pct": round(m.change_pct, 2)} for m in self.movers[:5]],
            "book": {"equity": round(book.equity, 2), "open_positions": book.open_symbols},
        }

    async def chief_round(self) -> None:
        if self.paused:
            return
        book = self.book()
        now = time.time()
        candidates = [
            st for st, ts in self.setups.values()
            if now - ts <= SETUP_MAX_AGE
            and st.symbol not in book.open_symbols
            and now - book.last_proposal_ts.get(st.symbol, 0) > self.s.symbol_cooldown_min * 60
            and not (self.reviewed.get(st.symbol, ("", 0))[0] == st.kind
                     and now - self.reviewed[st.symbol][1] <= SETUP_MAX_AGE)
        ]
        if not candidates:
            return
        if len(book.open_symbols) >= self.s.max_open_positions:
            self.store.note("RISK", f"book full ({self.s.max_open_positions}) · chief not asked")
            return
        calls_key = f"claude_calls:{utc_day()}"
        if self.claude_calls_today() >= self.s.claude_max_calls_per_day:
            self.store.note("CHIEF", "daily Claude call limit reached · waiting for tomorrow", level="warn")
            return
        self.store.set_meta(calls_key, self.claude_calls_today() + 1)

        mode = "live" if self.broker.mode == "live" else "paper (simulated money)"
        try:
            result: ChiefResult = await self.chief.review(self._chief_input(candidates), self.s.min_reward_risk, mode)
        except Exception as e:
            self.store.note("CHIEF", f"review failed: {e}", level="warn")
            return
        self.last_run["CHIEF"] = time.time()
        for c in candidates:
            self.reviewed[c.symbol] = (c.kind, now)
        if result.summary:
            self.store.note("CHIEF", result.summary)

        by_sym = {c.symbol.upper(): c for c in candidates}
        for d in result.decisions:
            st = by_sym.get(d.symbol)
            if not st:
                continue
            if d.action != "propose" or d.conviction < 3:
                self.store.note("CHIEF", f"pass · {d.thesis}", st.symbol)
                continue
            await self._propose(st, d)

    async def _propose(self, st: hunter.Setup, d) -> None:
        price = self.prices.get(st.symbol, st.price)
        stop = d.stop if 0 < d.stop < price else st.stop
        target = d.target if d.target > price else st.target
        idea = risk.Idea(st.symbol, price, stop, target, st.atr_pct)
        verdict = risk.check(idea, self.book(), self.s, live=self.broker.mode == "live")
        fields = dict(symbol=st.symbol, setup=st.kind, entry=price, stop=stop, target=target,
                      conviction=d.conviction, thesis=d.thesis, risks=d.risks)
        if not verdict.ok:
            self.store.add_proposal(**fields, qty=0.0, status="blocked", status_note=verdict.summary)
            self.store.note("RISK", f"no · {verdict.summary}", st.symbol, level="warn")
            return
        pid = self.store.add_proposal(**fields, qty=verdict.qty, status="pending")
        self.store.note("RISK", f"ok · size {verdict.qty:.6g} ({verdict.qty * price:,.0f} {self.s.quote})", st.symbol)
        self.store.note("CHIEF", f"needs your call · conviction {d.conviction}/5", st.symbol)
        rr = (target - price) / (price - stop)
        text = (
            f"<b>{esc(st.symbol)} · {esc(st.kind)}</b>  [{self.broker.mode.upper()}]\n"
            f"Entry ~{fmt_price(price)} · stop {fmt_price(stop)} · target {fmt_price(target)} · R/R {rr:.1f}\n"
            f"Size {verdict.qty:.6g} (~{verdict.qty * price:,.0f} {esc(self.s.quote)}) · conviction {d.conviction}/5\n\n"
            f"{esc(d.thesis)}\n<i>Risk: {esc(d.risks)}</i>\n\n"
            f"Expires in {self.s.proposal_ttl_min} min."
        )
        mid = await self.tg.ask(pid, text)
        if mid:
            self.store.set_proposal_message(pid, mid)

    # -- decisions from you --------------------------------------------------
    async def decide(self, pid: int, approve: bool) -> str:
        async with self._decide_lock:
            p = self.store.get_proposal(pid)
            if not p:
                return "Proposal not found."
            if not self.store.claim_pending(pid, "executing" if approve else "rejected"):
                return f"Already {p['status']}."
            if not approve:
                self.store.note("PM", "rejected by owner", p["symbol"])
                await self.tg.resolve(p["tg_message_id"], f"<b>{esc(p['symbol'])}</b> rejected.")
                return "Rejected."
            return await self._execute(p)

    async def _execute(self, p: dict) -> str:
        sym = p["symbol"]
        try:
            price = await self.market.last_price(sym)
        except Exception as e:
            self.store.set_proposal_status(p["id"], "failed", f"price unavailable: {e}")
            return "Could not get a price. Nothing was bought."
        self.prices[sym] = price
        ok, drift = risk.drift_ok(p["entry"], price, self.s)
        idea = risk.Idea(sym, price, p["stop"], p["target"], 0.0)
        verdict = risk.check(idea, self.book(), self.s, live=self.broker.mode == "live", check_cooldown=False)
        if not ok or not verdict.ok:
            why = f"price moved {drift:+.2f}%" if not ok else verdict.summary
            self.store.set_proposal_status(p["id"], "blocked", why)
            self.store.note("RISK", f"no at execution · {why}", sym, level="warn")
            await self.tg.resolve(p["tg_message_id"], f"<b>{esc(sym)}</b> approved but blocked: {esc(why)}.")
            return f"Blocked: {why}"
        qty = min(p["qty"], verdict.qty)
        try:
            fill = await self.broker.buy(sym, qty, price)
        except Exception as e:
            self.store.set_proposal_status(p["id"], "failed", str(e))
            self.store.note("PM", f"order failed · {e}", sym, level="warn")
            return f"Order failed: {e}"
        self.store.open_position(proposal_id=p["id"], symbol=sym, qty=fill.qty, entry=fill.price, stop=p["stop"],
                                 target=p["target"], entry_fee=fill.fee, mode=self.broker.mode)
        self.store.set_proposal_status(p["id"], "filled", f"bought {fill.qty:.6g} at {fmt_price(fill.price)}")
        self.store.note("PM", f"bought {fill.qty:.6g} at {fmt_price(fill.price)}", sym)
        await self.tg.resolve(p["tg_message_id"],
                              f"<b>{esc(sym)}</b> bought {fill.qty:.6g} at {fmt_price(fill.price)} "
                              f"[{self.broker.mode.upper()}]. Stop {fmt_price(p['stop'])}, target {fmt_price(p['target'])}.")
        return "Approved and filled."

    # -- position monitor ----------------------------------------------------
    async def monitor(self) -> None:
        now = time.time()
        for p in self.store.pending_proposals():
            if now - p["ts"] > self.s.proposal_ttl_min * 60 and self.store.claim_pending(p["id"], "expired"):
                self.store.note("PM", "proposal expired unanswered", p["symbol"])
                await self.tg.resolve(p["tg_message_id"], f"<b>{esc(p['symbol'])}</b> expired unanswered.")

        for pos in self.store.open_positions():
            try:
                price = await self.market.last_price(pos["symbol"])
            except Exception as e:
                log.warning("monitor %s: %s", pos["symbol"], e)
                continue
            self.prices[pos["symbol"]] = price
            reason = "stop" if price <= pos["stop"] else "target" if price >= pos["target"] else None
            if reason:
                await self.close_position(pos, price, reason)

        last_eq = float(self.store.get_meta("last_equity_ts", 0))
        if now - last_eq >= 300:
            self.store.record_equity(self.equity())
            self.store.set_meta("last_equity_ts", now)
        self.last_run["MONITOR"] = now

    async def close_position(self, pos: dict, price: float, reason: str) -> None:
        try:
            fill = await self.broker.sell(pos["symbol"], pos["qty"], price)
        except Exception as e:
            self.store.note("PM", f"exit order failed · {e}", pos["symbol"], level="warn")
            await self.tg.send(f"⚠ Exit for <b>{esc(pos['symbol'])}</b> failed: {esc(e)}. Check the exchange.")
            return
        pnl = (fill.price * fill.qty - fill.fee) - (pos["entry"] * pos["qty"] + pos["entry_fee"])
        self.store.close_position(pos["id"], fill.price, fill.fee, pnl, reason)
        self.store.note("PM", f"closed at {reason} · {fmt_price(fill.price)} · P/L {pnl:+,.2f}", pos["symbol"],
                        level="info" if pnl >= 0 else "warn")
        await self.tg.send(f"<b>{esc(pos['symbol'])}</b> closed at {reason}: {fmt_price(fill.price)}. "
                           f"P/L {pnl:+,.2f} {esc(self.s.quote)} [{self.broker.mode.upper()}]")

    # -- commands and summaries ---------------------------------------------
    def status_text(self) -> str:
        eq, start = self.equity(), self.day_start_equity()
        opens = self.store.open_positions()
        lines = [
            f"<b>Night Desk</b> [{self.broker.mode.upper()}]{' · PAUSED' if self.paused else ''}",
            f"Equity {eq:,.2f} {esc(self.s.quote)} · today {eq - start:+,.2f}",
            f"Open positions {len(opens)}/{self.s.max_open_positions} · Claude calls today "
            f"{self.claude_calls_today()}/{self.s.claude_max_calls_per_day}",
        ]
        for p in opens:
            last = self.prices.get(p["symbol"], p["entry"])
            lines.append(f"• {esc(p['symbol'])} {p['qty']:.6g} @ {fmt_price(p['entry'])} · now {fmt_price(last)} "
                         f"({(last / p['entry'] - 1) * 100:+.1f}%)")
        return "\n".join(lines)

    async def command(self, cmd: str) -> str:
        if cmd == "/pause":
            self.store.set_meta("paused", True)
            self.store.note("PM", "desk paused by owner · no new proposals")
            return "Paused. Open positions are still watched for stop and target."
        if cmd == "/resume":
            self.store.set_meta("paused", False)
            self.store.note("PM", "desk resumed by owner")
            return "Resumed."
        if cmd in ("/status", "/positions", "/start"):
            return self.status_text()
        return "Commands: /status, /pause, /resume"

    async def daily_summary(self) -> None:
        key = f"summary_sent:{utc_day()}"
        if datetime.now(timezone.utc).hour != self.s.daily_summary_hour_utc or self.store.get_meta(key):
            return
        self.store.set_meta(key, True)
        since = time.time() - 86400
        realised = self.store.realised_pnl_since(since)
        await self.tg.send(self.status_text() + f"\nRealised P/L last 24h {realised:+,.2f} {esc(self.s.quote)}")

    # -- dashboard snapshot --------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        eq = self.equity()
        opens = []
        for p in self.store.open_positions():
            last = self.prices.get(p["symbol"], p["entry"])
            opens.append({**p, "last": last,
                          "upnl": (last - p["entry"]) * p["qty"] - p["entry_fee"]})
        return {
            "mode": self.broker.mode,
            "dry_run": getattr(self.s, "live_dry_run", True) if self.broker.mode == "live" else None,
            "demo": self.s.demo,
            "paused": self.paused,
            "quote": self.s.quote,
            "equity": eq,
            "day_start_equity": self.day_start_equity(),
            "cash": self.broker.cash,
            "claude_calls_today": self.claude_calls_today(),
            "claude_max_calls": self.s.claude_max_calls_per_day,
            "news_enabled": bool(self.s.lunarcrush_api_key),
            "telegram_enabled": self.tg.enabled,
            "desks": self.store.latest_note_per_desk(),
            "last_run": self.last_run,
            "notes": self.store.recent_notes(60),
            "proposals": self.store.recent_proposals(20),
            "positions": opens,
            "closed": self.store.closed_positions(10),
            "equity_history": self.store.equity_history(time.time() - 7 * 86400),
            "movers": [vars(m) for m in self.movers],
            "setups": [st.as_dict() for st, _ in self.setups.values()],
            "server_time": time.time(),
        }
