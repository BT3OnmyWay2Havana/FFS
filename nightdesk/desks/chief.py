"""Chief of staff: Claude reads every desk's notes and decides which setups deserve your yes or no."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

log = logging.getLogger(__name__)

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "desk_summary": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["propose", "pass"]},
                    "conviction": {"type": "integer"},
                    "stop": {"type": "number"},
                    "target": {"type": "number"},
                    "thesis": {"type": "string"},
                    "risks": {"type": "string"},
                },
                "required": ["symbol", "action", "conviction", "stop", "target", "thesis", "risks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["desk_summary", "decisions"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You are the chief of staff on a small crypto spot trading desk. Automated desks have
already done the legwork: the hunter found rule-based long setups on hourly candles, the whale desk
summarised unusually large exchange trades, and the news desk (if present) added social sentiment.

Your job is to decide, for each setup listed, whether it is worth interrupting the owner for a yes
or no. Most setups should be passed. Propose only when the setup, the large-trade flow and the
sentiment (if any) point the same way, and say plainly what would make the trade wrong.

Rules:
- Only use symbols from the setups list. Return exactly one decision per setup.
- Long only, spot only. The stop must be below the current price and the target above it.
- You may tighten the suggested stop or move the target, but keep reward/risk at or above {min_rr}.
- conviction is an integer from 1 (weak) to 5 (strong). Propose only at 3 or above.
- thesis: two sentences at most, plain English. risks: one sentence.
- desk_summary: one or two sentences on the overall picture for the squawk feed.
- A separate risk desk will still check position size, volatility and loss limits after you.
- This is a {mode} account. Never claim a trade is safe or certain.

Desk data (JSON):
{data}
"""


@dataclass
class Decision:
    symbol: str
    action: str
    conviction: int
    stop: float
    target: float
    thesis: str
    risks: str


@dataclass
class ChiefResult:
    summary: str
    decisions: list[Decision]


class Chief(Protocol):
    async def review(self, data: dict[str, Any], min_rr: float, mode: str) -> ChiefResult: ...


def parse_result(raw: str, allowed_symbols: set[str]) -> ChiefResult:
    """Validate Claude's JSON. Drops anything malformed or outside the setups list."""
    obj = json.loads(raw)
    decisions: list[Decision] = []
    seen: set[str] = set()
    for d in obj.get("decisions", []):
        try:
            sym = str(d["symbol"]).upper()
            if sym not in allowed_symbols or sym in seen:
                continue
            action = d["action"] if d["action"] in ("propose", "pass") else "pass"
            conviction = max(1, min(5, int(d["conviction"])))
            decisions.append(Decision(sym, action, conviction, float(d["stop"]), float(d["target"]),
                                      str(d["thesis"])[:400], str(d["risks"])[:300]))
            seen.add(sym)
        except (KeyError, TypeError, ValueError):
            log.warning("Dropped malformed decision: %r", d)
    return ChiefResult(str(obj.get("desk_summary", ""))[:400], decisions)


class ClaudeChief:
    def __init__(self, api_key: str, model: str, effort: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=180.0)
        self.model = model
        self.effort = effort

    async def review(self, data: dict[str, Any], min_rr: float, mode: str) -> ChiefResult:
        prompt = INSTRUCTIONS.format(min_rr=min_rr, mode=mode, data=json.dumps(data, indent=1, default=str))
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this review")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        allowed = {s["symbol"].upper() for s in data.get("setups", [])}
        return parse_result(text, allowed)


class RuleChief:
    """Offline stand-in for demo mode: proposes clean setups that big money isn't selling into."""

    async def review(self, data: dict[str, Any], min_rr: float, mode: str) -> ChiefResult:
        flows = {f["symbol"]: f for f in data.get("whale_flow", [])}
        out = []
        for s in data.get("setups", []):
            bias = flows.get(s["symbol"], {}).get("bias", "quiet")
            good = s["reward_risk"] >= min_rr and bias != "selling"
            out.append(Decision(
                s["symbol"], "propose" if good else "pass", 3 if good else 1, s["stop"], s["target"],
                f"Demo rule: {s['kind']} with reward/risk {s['reward_risk']}, large trades {bias}.",
                "Demo mode uses simulated prices; this is not a real signal.",
            ))
        return ChiefResult("Demo mode: rule-based chief, no Claude call made.", out)
