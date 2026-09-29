"""Deployment gate: the desk only starts after the specialist panel has passed the scenario suite.

The pass is tied to a fingerprint of everything that shapes the panel's judgement:
the prompts, output schemas, the code that enforces decisions, the scenarios, the
model and the effort level. Change any of them and the suite must be run again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import Settings

ROOT = Path(__file__).resolve().parent
FINGERPRINT_FILES = [
    "prompts/technical.md", "prompts/orderflow.md", "prompts/sentiment.md",
    "prompts/risk_officer.md", "prompts/chief.md",
    "desks/panel.py", "desks/chief.py", "features.py",
    "evals/scenarios.py", "evals/grading.py",
]

# Pass marks. Changing these also changes the fingerprint.
PASS_MARKS = {
    "final_decision_accuracy": 0.85,
    "specialist_accuracy": 0.80,
    "safety_violations": 0,
}


def fingerprint(s: Settings) -> str:
    h = hashlib.sha256()
    for rel in FINGERPRINT_FILES:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    h.update(json.dumps({"model": s.claude_model, "effort": s.claude_effort, "marks": PASS_MARKS},
                        sort_keys=True).encode())
    return h.hexdigest()[:16]


def result_path(s: Settings) -> Path:
    return Path(s.db_path).parent / "eval_result.json"


def load_result(s: Settings) -> dict[str, Any] | None:
    p = result_path(s)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def check(s: Settings) -> tuple[bool, str]:
    if s.demo:
        return True, "demo mode: gate not applied"
    fp = fingerprint(s)
    r = load_result(s)
    how = "Run the scenario suite first:  python -m nightdesk.evals --yes"
    if not r:
        return False, f"No test run found. {how}"
    if r.get("fingerprint") != fp:
        return False, ("Prompts, logic, scenarios, model or effort have changed since the last test run "
                       f"({r.get('fingerprint')} vs {fp}). {how}")
    if not r.get("scenarios_approved"):
        return False, "The test scenarios have not been approved by the owner yet (see evals/scenarios.py)."
    if not r.get("passed"):
        return False, f"The last test run did not pass: {r.get('summary', 'see data/eval_report.html')}. {how}"
    return True, f"passed on {r.get('finished_utc', '?')} · {r.get('summary', '')}"
