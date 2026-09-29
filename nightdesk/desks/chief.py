"""Chief of staff types and parsing. The Claude-backed chief lives in panel.py; RuleChief is the offline demo stand-in."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

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

@dataclass
class Decision:
    symbol: str
    action: str
    conviction: int
    stop: float
    target: float
    thesis: str
    risks: str
    size_multiplier: float = 1.0


@dataclass
class ChiefResult:
    summary: str
    decisions: list[Decision]
    reports: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


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


class RuleChief:
    """Offline stand-in for demo mode: proposes clean setups that big money isn't selling into."""

    async def review(self, data: dict[str, Any], min_rr: float, mode: str) -> ChiefResult:
        out = []
        for s in data.get("setups", []):
            st = s["setup"]
            bias = (s.get("flow") or {}).get("bias", "quiet")
            good = st["reward_risk"] >= min_rr and bias != "selling"
            out.append(Decision(
                s["symbol"], "propose" if good else "pass", 3 if good else 1, st["stop"], st["target"],
                f"Demo rule: {st['kind']} with reward/risk {st['reward_risk']}, large trades {bias}.",
                "Demo mode uses simulated prices; this is not a real signal.",
            ))
        return ChiefResult("Demo mode: rule-based chief, no Claude call made.", out)
