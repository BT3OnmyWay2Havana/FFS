import json

import pytest

from nightdesk.config import LIVE_CONFIRM_PHRASE, Settings
from nightdesk.desks import hunter, news, risk, scanner, whale
from nightdesk.desks.chief import parse_result
from nightdesk.indicators import atr, ema, rsi
from nightdesk.market import Candle, Ticker, Trade
from nightdesk.telegram import parse_callback


def S(**kw) -> Settings:
    base = dict(dashboard_token="x" * 20, anthropic_api_key="k")
    base.update(kw)
    return Settings(**base)


# -- indicators ----------------------------------------------------------------
def test_rsi_bounds_and_length():
    closes = [100 + (i % 7) - 3 + i * 0.1 for i in range(100)]
    r = rsi(closes, 14)
    assert len(r) == len(closes)
    assert all(0 <= v <= 100 for v in r)
    assert rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], 14)[-1] == 100.0


def test_ema_tracks_constant_series():
    assert ema([5.0] * 30, 10)[-1] == pytest.approx(5.0)


def test_atr_simple():
    highs, lows, closes = [11] * 20, [9] * 20, [10] * 20
    assert atr(highs, lows, closes, 14) == pytest.approx(2.0)


# -- scanner -------------------------------------------------------------------
def test_scanner_drops_illiquid_and_ranks():
    t = {
        "A/USD": Ticker("A/USD", 1, 12.0, 50e6),
        "B/USD": Ticker("B/USD", 1, 1.0, 900e6),
        "C/USD": Ticker("C/USD", 1, 30.0, 1e3),  # illiquid
    }
    movers = scanner.rank_movers(t, 5e6, 5)
    assert [m.symbol for m in movers] == ["A/USD", "B/USD"] or [m.symbol for m in movers] == ["B/USD", "A/USD"]
    assert "C/USD" not in {m.symbol for m in movers}
    watch = scanner.watch_symbols(movers, ["BTC", "ZZZ"], "USD", {"BTC/USD", "A/USD", "B/USD"})
    assert watch[0] == "BTC/USD" and "ZZZ/USD" not in watch


# -- hunter --------------------------------------------------------------------
def _candles(closes, vols=None):
    vols = vols or [100.0] * len(closes)
    return [Candle(i, c, c * 1.002, c * 0.998, c, v) for i, (c, v) in enumerate(zip(closes, vols))]


def test_hunter_finds_breakout_on_volume():
    closes = [100 + i * 0.05 for i in range(150)]
    closes.append(closes[-1] * 1.03)
    vols = [100.0] * 150 + [400.0]
    s = hunter.find_setup("X/USD", _candles(closes, vols))
    assert s and s.kind == "breakout"
    assert s.stop < s.price < s.target
    assert s.reward_risk == pytest.approx(2.0)


def test_hunter_ignores_flat_market():
    closes = [100.0 + (0.01 if i % 2 else -0.01) for i in range(150)]
    assert hunter.find_setup("X/USD", _candles(closes)) is None


def test_hunter_needs_enough_bars():
    assert hunter.find_setup("X/USD", _candles([100.0] * 50)) is None


# -- whale ---------------------------------------------------------------------
def test_whale_flags_only_large_prints_once():
    trades = [Trade(str(i), i, "buy", 100.0, 1.0) for i in range(300)]
    trades += [Trade("big1", 999, "buy", 100.0, 5000.0), Trade("big2", 1000, "sell", 100.0, 3000.0)]
    seen: set[str] = set()
    flow, fresh = whale.analyse("X/USD", trades, 100_000, seen)
    assert flow.big_buys == 1 and flow.big_sells == 1
    assert flow.bias == "two-way"
    assert {t.id for t in fresh} == {"big1", "big2"}
    _, fresh2 = whale.analyse("X/USD", trades, 100_000, seen)
    assert fresh2 == []


# -- news ----------------------------------------------------------------------
def test_news_parses_v4_list_and_headlines():
    payload = {"data": [
        {"symbol": "btc", "galaxy_score": 71, "galaxy_score_previous": 60, "alt_rank": 5, "alt_rank_previous": 90,
         "sentiment": 85, "social_volume_24h": 1000, "interactions_24h": 5e6, "social_dominance": 20},
        {"symbol": "XYZ", "galaxy_score": 10},
    ]}
    out = news.parse_coins(payload, {"BTC"})
    assert set(out) == {"BTC"}
    line = news.headline(out["BTC"])
    assert "galaxy 71 (+11)" in line and "altrank 5 (+85 places)" in line and "sentiment 85" in line


# -- risk ----------------------------------------------------------------------
def book(**kw):
    base = dict(equity=10_000, cash=10_000, day_start_equity=10_000, open_symbols=[], last_proposal_ts={})
    base.update(kw)
    return risk.Book(**base)


def test_risk_sizes_by_stop_distance_and_caps():
    s = S()
    v = risk.check(risk.Idea("X/USD", 100, 95, 110, 2.0), book(), s)
    assert v.ok
    # 1% of 10k = 100 at risk / 5 per unit = 20 units, but 10% cap = 1000/100 = 10 units
    assert v.qty == pytest.approx(10)


def test_risk_says_no_for_each_limit():
    s = S()
    assert not risk.check(risk.Idea("X/USD", 100, 101, 110, 2), book(), s).ok  # stop above entry
    assert "reward/risk" in risk.check(risk.Idea("X/USD", 100, 95, 102, 2), book(), s).summary
    assert "volatile" in risk.check(risk.Idea("X/USD", 100, 95, 110, 20), book(), s).summary
    assert "holding" in risk.check(risk.Idea("X/USD", 100, 95, 110, 2), book(open_symbols=["X/USD"]), s).summary
    full = book(open_symbols=["A", "B", "C", "D"])
    assert "book full" in risk.check(risk.Idea("X/USD", 100, 95, 110, 2), full, s).summary
    down = book(equity=9_600)
    assert "daily loss" in risk.check(risk.Idea("X/USD", 100, 95, 110, 2), down, s).summary
    import time
    recent = book(last_proposal_ts={"X/USD": time.time() - 60})
    assert "cooldown" in risk.check(risk.Idea("X/USD", 100, 95, 110, 2), recent, s).summary


def test_live_orders_capped_by_live_max():
    s = S(live_max_order_value=50)
    v = risk.check(risk.Idea("X/USD", 100, 95, 110, 2), book(), s, live=True)
    assert v.ok and v.qty * 100 == pytest.approx(50)


def test_drift_check():
    s = S(max_price_drift_pct=1.0)
    assert risk.drift_ok(100, 100.9, s)[0]
    assert not risk.drift_ok(100, 101.5, s)[0]


# -- chief parsing ---------------------------------------------------------------
def test_chief_parse_filters_unknown_and_clamps():
    raw = json.dumps({"desk_summary": "Quiet.", "decisions": [
        {"symbol": "btc/usd", "action": "propose", "conviction": 9, "stop": 1, "target": 3, "thesis": "t", "risks": "r"},
        {"symbol": "FAKE/USD", "action": "propose", "conviction": 5, "stop": 1, "target": 3, "thesis": "t", "risks": "r"},
        {"symbol": "ETH/USD", "action": "propose"},
    ]})
    res = parse_result(raw, {"BTC/USD", "ETH/USD"})
    assert [d.symbol for d in res.decisions] == ["BTC/USD"]
    assert res.decisions[0].conviction == 5


# -- config and telegram ---------------------------------------------------------
def test_live_needs_all_switches():
    assert not S(trading_mode="live").live_enabled
    assert not S(trading_mode="live", live_confirm=LIVE_CONFIRM_PHRASE).live_enabled
    assert S(trading_mode="live", live_confirm=LIVE_CONFIRM_PHRASE, exchange_api_key="a", exchange_api_secret="b").live_enabled
    assert any("Refusing" in p for p in S(trading_mode="live").problems())


def test_callback_parsing():
    assert parse_callback("yes:12") == (12, True)
    assert parse_callback("no:3") == (3, False)
    assert parse_callback("maybe:3") is None
    assert parse_callback("yes:abc") is None
