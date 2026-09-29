"""End-to-end desk flow on simulated prices, with fake chief and Telegram."""

import asyncio

import pytest

from nightdesk.brokers.paper import PaperBroker
from nightdesk.config import Settings
from nightdesk.desk import Desk
from nightdesk.desks.chief import ChiefResult, Decision
from nightdesk.desks.hunter import Setup
from nightdesk.market import SimMarket
from nightdesk.store import Store


class FakeTG:
    enabled = True

    def __init__(self):
        self.asked, self.sent, self.resolved = [], [], []

    async def ask(self, pid, text):
        self.asked.append((pid, text))
        return 100 + pid

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return 1

    async def resolve(self, mid, text):
        self.resolved.append((mid, text))


class YesChief:
    def __init__(self):
        self.calls = 0

    async def review(self, data, min_rr, mode):
        self.calls += 1
        return ChiefResult("test summary", [
            Decision(s["symbol"], "propose", 4, s["stop"], s["target"], "thesis", "risk") for s in data["setups"]
        ])


def make_desk(**kw):
    s = Settings(dashboard_token="x" * 20, demo=True, **kw)
    store = Store(":memory:")
    market = SimMarket("USD")
    tg = FakeTG()
    chief = YesChief()
    desk = Desk(s, store, market, chief, PaperBroker(store, s), tg)
    return desk, market, tg, chief


def plant_setup(desk, market, sym="BTC/USD"):
    price = market.hist[sym][-1].close
    desk.prices[sym] = price
    st = Setup(sym, "breakout", price, price * 0.97, price * 1.06, 2.0, 60.0, 2.0, "up", "test")
    import time
    desk.setups[sym] = (st, time.time())
    return st


@pytest.fixture
def run():
    return lambda coro: asyncio.run(coro)


def test_full_round_propose_approve_and_stop_out(run):
    desk, market, tg, chief = make_desk()
    plant_setup(desk, market)
    run(desk.chief_round())
    pending = desk.store.pending_proposals()
    assert len(pending) == 1 and tg.asked
    pid = pending[0]["id"]

    # double approval only fills once
    msg1 = run(desk.decide(pid, True))
    msg2 = run(desk.decide(pid, True))
    assert "filled" in msg1.lower() and "already" in msg2.lower()
    opens = desk.store.open_positions()
    assert len(opens) == 1
    assert desk.broker.cash < desk.s.start_equity

    # force a stop-out
    market.hist["BTC/USD"][-1].close = opens[0]["stop"] * 0.99
    run(desk.monitor())
    assert not desk.store.open_positions()
    closed = desk.store.closed_positions()
    assert closed[0]["close_reason"] == "stop" and closed[0]["pnl"] < 0
    assert desk.broker.cash == pytest.approx(desk.s.start_equity + closed[0]["pnl"])


def test_same_setup_not_reviewed_twice(run):
    desk, market, tg, chief = make_desk()
    plant_setup(desk, market)
    run(desk.chief_round())
    run(desk.chief_round())
    assert chief.calls == 1


def test_claude_budget_respected(run):
    desk, market, tg, chief = make_desk(claude_max_calls_per_day=0)
    plant_setup(desk, market)
    run(desk.chief_round())
    assert chief.calls == 0


def test_reject_and_expiry(run):
    desk, market, tg, chief = make_desk(proposal_ttl_min=0)
    plant_setup(desk, market, "BTC/USD")
    plant_setup(desk, market, "ETH/USD")
    run(desk.chief_round())
    p1, p2 = desk.store.pending_proposals()
    assert run(desk.decide(p1["id"], False)) == "Rejected."
    run(desk.monitor())
    assert desk.store.get_proposal(p2["id"])["status"] == "expired"
    assert not desk.store.open_positions()


def test_pause_blocks_new_proposals(run):
    desk, market, tg, chief = make_desk()
    run(desk.command("/pause"))
    plant_setup(desk, market)
    run(desk.chief_round())
    assert chief.calls == 0
    run(desk.command("/resume"))
    run(desk.chief_round())
    assert chief.calls == 1


def test_price_drift_blocks_at_approval(run):
    desk, market, tg, chief = make_desk()
    plant_setup(desk, market)
    run(desk.chief_round())
    pid = desk.store.pending_proposals()[0]["id"]
    market.hist["BTC/USD"][-1].close *= 1.05
    assert run(desk.decide(pid, True)).startswith("Blocked")
    assert not desk.store.open_positions()


def test_scan_hunt_and_whale_run_offline(run):
    desk, market, tg, chief = make_desk()
    run(desk.scan_and_hunt())
    run(desk.watch_whales())
    snap = desk.snapshot()
    assert snap["movers"] and "SCAN" in snap["desks"] and "HUNT" in snap["desks"]
