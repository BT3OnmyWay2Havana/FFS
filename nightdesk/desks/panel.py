"""The specialist panel: four Claude analysts report in parallel, then the chief decides.

Code, not the model, has the last word on safety: a technical `against` or a risk
officer `veto` always means pass, missing reports fail safe, and the risk officer
can only shrink a size.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .chief import DECISION_SCHEMA, ChiefResult, Decision, parse_result

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Published per-million-token prices, used only to report spend.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00)}


def _schema(item_props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}, **item_props},
                    "required": ["symbol", *item_props.keys()],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assessments"],
        "additionalProperties": False,
    }


STANCE4 = {"type": "string", "enum": ["supportive", "neutral", "against", "insufficient_data"]}

SPECIALISTS: dict[str, dict[str, Any]] = {
    "technical": {
        "prompt": "technical.md",
        "schema": _schema({
            "stance": {"type": "string", "enum": ["supportive", "neutral", "against"]},
            "quality": {"type": "integer"},
            "structural_stop": {"type": "number"},
            "target": {"type": "number"},
            "key_levels": {"type": "string"},
            "invalidation": {"type": "string"},
            "notes": {"type": "string"},
        }),
    },
    "orderflow": {
        "prompt": "orderflow.md",
        "schema": _schema({"stance": STANCE4, "confidence": {"type": "integer"}, "read": {"type": "string"}}),
    },
    "sentiment": {
        "prompt": "sentiment.md",
        "schema": _schema({"stance": STANCE4, "confidence": {"type": "integer"}, "read": {"type": "string"}}),
    },
    "risk_officer": {
        "prompt": "risk_officer.md",
        "schema": _schema({
            "verdict": {"type": "string", "enum": ["clear", "reduce", "veto"]},
            "size_multiplier": {"type": "number"},
            "concerns": {"type": "string"},
        }),
    },
}

# What a missing or unreadable report counts as. Every default errs towards not trading.
FAIL_SAFE: dict[str, dict[str, Any]] = {
    "technical": {"stance": "against", "quality": 1, "notes": "No technical report; treated as against."},
    "orderflow": {"stance": "insufficient_data", "confidence": 1, "read": "No order-flow report."},
    "sentiment": {"stance": "insufficient_data", "confidence": 1, "read": "No sentiment data."},
    "risk_officer": {"verdict": "veto", "size_multiplier": 0.0, "concerns": "No risk officer report; vetoed."},
}


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text()


def all_prompt_texts() -> dict[str, str]:
    files = [spec["prompt"] for spec in SPECIALISTS.values()] + ["chief.md"]
    return {f: load_prompt(f) for f in files}


def clamp_multiplier(verdict: str, value: Any) -> float:
    if verdict == "veto":
        return 0.0
    if verdict == "clear":
        return 1.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.5
    return max(0.25, min(0.75, v))


def normalise(name: str, raw: dict[str, Any], symbols: set[str]) -> dict[str, dict[str, Any]]:
    """Index a specialist's assessments by symbol, fill gaps with fail-safe defaults."""
    out: dict[str, dict[str, Any]] = {}
    for a in raw.get("assessments", []) if isinstance(raw, dict) else []:
        sym = str(a.get("symbol", "")).upper()
        if sym in symbols and sym not in out:
            out[sym] = dict(a, symbol=sym)
    for sym in symbols:
        out.setdefault(sym, dict(FAIL_SAFE[name], symbol=sym, missing=True))
    if name == "risk_officer":
        for a in out.values():
            if a.get("verdict") not in ("clear", "reduce", "veto"):
                a["verdict"] = "veto"
            a["size_multiplier"] = clamp_multiplier(a["verdict"], a.get("size_multiplier"))
    return out


def enforce(decisions: list[Decision], reports: dict[str, dict[str, dict[str, Any]]]) -> list[Decision]:
    """Apply the non-negotiable rules to the chief's decisions."""
    for d in decisions:
        tech = reports["technical"][d.symbol]
        risk = reports["risk_officer"][d.symbol]
        d.size_multiplier = risk["size_multiplier"]
        blockers = []
        if tech.get("stance") == "against":
            blockers.append("technical analyst against")
        if risk.get("verdict") == "veto":
            blockers.append("risk officer veto")
        if d.action == "propose" and d.conviction < 3:
            blockers.append("conviction below 3")
        if d.action == "propose" and blockers:
            d.action = "pass"
            d.thesis = f"Overruled ({', '.join(blockers)}). {d.thesis}"
    return decisions


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, u: Any) -> None:
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        inp = int(getattr(u, "input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
        # 1-hour cache writes bill at 2x input, reads at 0.05x on Opus 5.5 (approximate for other models).
        self.cost_usd += (inp * pin + out * pout + cw * pin * 2 + cr * pin * 0.05) / 1_000_000


class ClaudePanel:
    def __init__(self, api_key: str, model: str, effort: str, client: anthropic.AsyncAnthropic | None = None):
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=300.0)
        self.model = model
        self.effort = effort
        self.usage = Usage()
        self.last_reports: dict[str, dict[str, dict[str, Any]]] = {}

    async def _ask(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        self.usage.add(getattr(response, "model", self.model) or self.model, response.usage)
        if response.stop_reason == "refusal":
            raise RuntimeError("declined by Claude")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("answer cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)

    async def _specialist(self, name: str, payload: str, symbols: set[str]) -> dict[str, dict[str, Any]]:
        spec = SPECIALISTS[name]
        try:
            raw = await self._ask(load_prompt(spec["prompt"]), payload, spec["schema"])
        except Exception as e:
            log.warning("%s analyst failed: %s", name, e)
            raw = {}
        return normalise(name, raw, symbols)

    async def review(self, data: dict[str, Any], min_rr: float, mode: str) -> ChiefResult:
        symbols = {s["symbol"].upper() for s in data.get("setups", [])}
        payload = "Setups to assess, with desk context (JSON):\n" + json.dumps(data, indent=1, default=str)

        names = ["technical", "orderflow", "risk_officer"]
        has_sentiment = any(s.get("sentiment") for s in data.get("setups", []))
        if has_sentiment:
            names.append("sentiment")
        results = await asyncio.gather(*(self._specialist(n, payload, symbols) for n in names))
        reports = dict(zip(names, results))
        if not has_sentiment:
            reports["sentiment"] = normalise("sentiment", {}, symbols)
        self.last_reports = reports

        chief_input = dict(data, specialist_reports={
            sym: {name: {k: v for k, v in reports[name][sym].items() if k != "symbol"} for name in reports}
            for sym in symbols
        })
        chief_prompt = load_prompt("chief.md").replace("{mode}", mode)
        raw = await self._ask(chief_prompt, "Setups, facts and specialist reports (JSON):\n"
                              + json.dumps(chief_input, indent=1, default=str), DECISION_SCHEMA)
        result = parse_result(json.dumps(raw), symbols)
        result.decisions = enforce(result.decisions, reports)
        result.reports = reports
        return result
