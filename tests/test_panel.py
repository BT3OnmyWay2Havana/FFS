"""Specialist panel, deployment gate, scenario suite and grader. No real Claude calls."""

import asyncio
import json
import shutil
from pathlib import Path

import anthropic
import httpx2
import pytest

from nightdesk import features, gate
from nightdesk.config import Settings
from nightdesk.desks.chief import ChiefResult, Decision
from nightdesk.desks.panel import ClaudePanel, clamp_multiplier, normalise
from nightdesk.evals import scenarios as sc_mod
from nightdesk.evals.grading import grade, summarise
from nightdesk.evals.report import review_page, write_report
from nightdesk.market import SimMarket

# -- a fake Claude that answers by recognising which prompt it was given --------


def fake_client(answers: dict[str, dict], seen: list):
    def handler(req):
        body = json.loads(req.content)
        system = body["system"][0]["text"]
        who = ("technical" if "technical analyst" in system[:200] else
               "orderflow" if "order-flow analyst" in system[:200] else
               "sentiment" if "sentiment analyst" in system[:200] else
               "risk_officer" if "risk officer" in system[:200] else "chief")
        seen.append((who, body))
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(answers.get(who, {}))}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        })
    return anthropic.AsyncAnthropic(api_key="sk-test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


def chief_says(sym, action="propose", conviction=4, stop=140.0, target=170.0):
    return {"desk_summary": "s", "decisions": [
        {"symbol": sym, "action": action, "conviction": conviction, "stop": stop, "target": target,
         "thesis": "t", "risks": "r"}]}


def assess(sym, **kw):
    return {"assessments": [dict(symbol=sym, **kw)]}


SC = {x.id: x for x in sc_mod.SCENARIOS}


def run_panel(answers, data):
    seen = []
    panel = ClaudePanel("sk-test", "claude-opus-5-5", "medium", client=fake_client(answers, seen))
    res = asyncio.run(panel.review(data, 1.5, "paper"))
    return res, seen, panel


GOOD = {
    "technical": assess("SOL/USD", stance="supportive", quality=4, structural_stop=148, target=160,
                        key_levels="k", invalidation="i", notes="n"),
    "orderflow": assess("SOL/USD", stance="supportive", confidence=3, read="r"),
    "risk_officer": assess("SOL/USD", verdict="clear", size_multiplier=1.0, concerns="None."),
    "chief": chief_says("SOL/USD", stop=148, target=160),
}


def test_panel_happy_path_and_call_shape():
    res, seen, panel = run_panel(GOOD, SC["clean_breakout"].data)
    assert [d.action for d in res.decisions] == ["propose"]
    who = sorted(w for w, _ in seen)
    assert who == ["chief", "orderflow", "risk_officer", "technical"]  # no sentiment data -> no sentiment call
    body = seen[0][1]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    chief_body = next(b for w, b in seen if w == "chief")
    assert "specialist_reports" in chief_body["messages"][0]["content"]
    assert panel.usage.calls == 4 and panel.usage.cost_usd > 0


def test_code_overrules_chief_when_technical_against():
    answers = dict(GOOD, technical=assess("SOL/USD", stance="against", quality=2, structural_stop=148, target=150,
                                          key_levels="k", invalidation="i", notes="n"))
    res, _, _ = run_panel(answers, SC["clean_breakout"].data)
    assert res.decisions[0].action == "pass" and res.decisions[0].thesis.startswith("Overruled")


def test_code_overrules_chief_on_veto_and_missing_risk_report():
    res, _, _ = run_panel(dict(GOOD, risk_officer=assess("SOL/USD", verdict="veto", size_multiplier=1, concerns="c")),
                          SC["clean_breakout"].data)
    assert res.decisions[0].action == "pass"
    res, _, _ = run_panel(dict(GOOD, risk_officer={}), SC["clean_breakout"].data)
    assert res.decisions[0].action == "pass"
    assert res.reports["risk_officer"]["SOL/USD"]["verdict"] == "veto"


def test_missing_technical_report_fails_safe():
    res, _, _ = run_panel(dict(GOOD, technical={}), SC["clean_breakout"].data)
    assert res.decisions[0].action == "pass"


def test_risk_officer_can_only_shrink():
    assert clamp_multiplier("clear", 3.0) == 1.0
    assert clamp_multiplier("reduce", 5.0) == 0.75
    assert clamp_multiplier("reduce", 0.01) == 0.25
    assert clamp_multiplier("veto", 1.0) == 0.0
    res, _, _ = run_panel(dict(GOOD, risk_officer=assess("SOL/USD", verdict="reduce", size_multiplier=9, concerns="c")),
                          SC["clean_breakout"].data)
    assert res.decisions[0].size_multiplier == 0.75


def test_invented_symbols_are_ignored():
    rep = normalise("orderflow", assess("FAKE/USD", stance="supportive", confidence=5, read="x"), {"SOL/USD"})
    assert set(rep) == {"SOL/USD"} and rep["SOL/USD"]["stance"] == "insufficient_data"


def test_sentiment_called_only_with_data():
    answers = dict(GOOD, sentiment=assess("LINK/USD", stance="supportive", confidence=2, read="r"))
    _, seen, _ = run_panel(answers, SC["new_30d_high_with_attention"].data)
    assert "sentiment" in [w for w, _ in seen]


# -- scenario suite integrity ----------------------------------------------------


def test_scenarios_match_live_feature_shape():
    m = SimMarket()
    live_h = features.hourly(asyncio.run(m.candles("SOL/USD", "1h", 200)))
    for x in sc_mod.SCENARIOS:
        assert set(x.data) == {"time_utc", "weekday", "quote_currency", "btc_context", "book", "limits", "setups"}
        for c in x.data["setups"]:
            assert set(c["hourly"]) == set(live_h), x.id
            st = c["setup"]
            assert st["stop"] < st["price"] < st["target"], x.id
        assert set(x.expect) <= {c["symbol"] for c in x.data["setups"]}, x.id
        json.dumps(x.data)


def test_scenarios_have_unique_ids_and_balance():
    ids = [x.id for x in sc_mod.SCENARIOS]
    assert len(ids) == len(set(ids)) and len(ids) >= 25
    finals = [e.final for x in sc_mod.SCENARIOS for e in x.expect.values() if e.final]
    assert finals.count("propose") >= 8 and finals.count("pass") >= 10


# -- grading -------------------------------------------------------------------


def perfect_result(x):
    reports = {n: {} for n in ("technical", "orderflow", "sentiment", "risk_officer")}
    decisions = []
    for c in x.data["setups"]:
        sym, ex = c["symbol"], x.expect.get(c["symbol"])
        pick = lambda allowed, dflt: sorted(allowed)[0] if allowed else dflt
        reports["technical"][sym] = {"stance": pick(ex and ex.technical, "neutral")}
        reports["orderflow"][sym] = {"stance": pick(ex and ex.orderflow, "neutral")}
        reports["sentiment"][sym] = {"stance": pick(ex and ex.sentiment, "insufficient_data")}
        reports["risk_officer"][sym] = {"verdict": pick(ex and ex.risk_officer, "clear")}
        st = c["setup"]
        action = (ex.final if ex and ex.final else "pass")
        decisions.append(Decision(sym, action, 4 if action == "propose" else 1, st["stop"], st["target"], "t", "r"))
    return ChiefResult("s", decisions, reports)


def test_grader_full_marks_for_expected_answers():
    graded = [grade(x, perfect_result(x)) for x in sc_mod.SCENARIOS]
    s = summarise(graded)
    assert s["final_decision_accuracy"] == 1.0 and s["specialist_accuracy"] == 1.0 and s["safety_violations"] == 0


def test_grader_flags_injection_and_bad_levels():
    x = SC["injection_in_data"]
    r = perfect_result(x)
    r.decisions[0].action = "propose"
    g = grade(x, r)
    assert any("hidden" in v for v in g["safety"]) and g["final_ok"] == 0
    x = SC["clean_breakout"]
    r = perfect_result(x)
    r.decisions[0].stop = 1e9
    assert any("wrong side" in v for v in grade(x, r)["safety"])
    r = perfect_result(x)
    r.decisions = []
    assert any("no decision" in v for v in grade(x, r)["safety"])


def test_crash_counts_as_failure():
    g = grade(SC["clean_breakout"], None, error="boom")
    assert g["final_ok"] == 0 and g["final_total"] == 1


def test_reports_render(tmp_path):
    graded = []
    for x in sc_mod.SCENARIOS:
        r = perfect_result(x)
        g = grade(x, r)
        g["result"] = {"summary": "s", "decisions": [vars(d) for d in r.decisions], "reports": r.reports}
        graded.append(g)
    result = {"passed": True, "summary": "x", "scores": summarise(graded), "model": "m", "effort": "e",
              "fingerprint": "f", "finished_utc": "now", "scenarios_approved": False,
              "usage": {"calls": 0, "estimated_cost_usd": 0.0}}
    p = write_report(tmp_path / "r.html", result, graded, sc_mod.SCENARIOS)
    assert "PASSED" in p.read_text()
    page = review_page(sc_mod.SCENARIOS)
    assert page.startswith("<title>") and "<html" not in page and "SYSTEM NOTE" in page


# -- gate ------------------------------------------------------------------------


def S(tmp_path, **kw):
    return Settings(dashboard_token="x" * 20, anthropic_api_key="k", db_path=str(tmp_path / "db.sqlite3"), **kw)


def test_gate_blocks_until_matching_approved_pass(tmp_path):
    s = S(tmp_path)
    assert gate.check(S(tmp_path, demo=True))[0]
    ok, why = gate.check(s)
    assert not ok and "No test run" in why
    base = {"fingerprint": gate.fingerprint(s), "passed": True, "scenarios_approved": False, "summary": "x"}
    gate.result_path(s).write_text(json.dumps(base))
    assert "not been approved" in gate.check(s)[1]
    gate.result_path(s).write_text(json.dumps(dict(base, scenarios_approved=True, passed=False)))
    assert "did not pass" in gate.check(s)[1]
    gate.result_path(s).write_text(json.dumps(dict(base, scenarios_approved=True)))
    assert gate.check(s)[0]
    assert not gate.check(S(tmp_path, claude_effort="high"))[0]  # different effort -> retest


def test_fingerprint_changes_with_prompt(tmp_path, monkeypatch):
    copy = tmp_path / "pkg"
    shutil.copytree(gate.ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(gate, "ROOT", copy)
    s = S(tmp_path)
    before = gate.fingerprint(s)
    p = copy / "prompts" / "risk_officer.md"
    p.write_text(p.read_text() + "\nextra")
    assert gate.fingerprint(s) != before
