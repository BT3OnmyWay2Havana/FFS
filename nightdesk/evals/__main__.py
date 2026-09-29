"""Run the scenario suite against the live Claude panel and record the result for the deployment gate.

    python -m nightdesk.evals            # shows the plan and estimated cost, sends nothing
    python -m nightdesk.evals --yes      # runs it (spends Claude API credit)
    python -m nightdesk.evals --yes --only clean_breakout,absorption
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone

from .. import config, gate
from ..desks.panel import ClaudePanel
from . import scenarios as sc_mod
from .grading import grade, summarise
from .report import write_report

# Rough per-call estimate for Opus 5.5 at medium effort: ~5k input and ~3k output tokens.
EST_COST_PER_CALL = (5_000 * 4 + 3_000 * 20) / 1_000_000


async def run_one(panel: ClaudePanel, sc, sem: asyncio.Semaphore):
    async with sem:
        t0 = time.time()
        try:
            res = await panel.review(sc.data, sc.data["limits"]["min_reward_risk"], "paper (simulated money)")
            g = grade(sc, res)
            g["result"] = {"summary": res.summary, "decisions": [vars(d) for d in res.decisions],
                           "reports": res.reports}
        except Exception as e:  # a crash counts as a failed case, never as a pass
            g = grade(sc, None, error=f"{type(e).__name__}: {e}")
            g["result"] = None
        g["seconds"] = round(time.time() - t0, 1)
        mark = "ok " if g["final_ok"] == g["final_total"] and g["spec_ok"] == g["spec_total"] and not g["safety"] else "MISS"
        print(f"  {mark} {sc.id:<30} final {g['final_ok']}/{g['final_total']}  specialists {g['spec_ok']}/{g['spec_total']}"
              f"{'  SAFETY: ' + '; '.join(g['safety']) if g['safety'] else ''}{'  ERROR: ' + g['error'] if g['error'] else ''}")
        return g


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually run (spends API credit)")
    ap.add_argument("--only", default="", help="comma-separated scenario ids (a partial run never unlocks the gate)")
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    config.load_dotenv()
    s = config.from_env()
    chosen = [x for x in sc_mod.SCENARIOS if not args.only or x.id in args.only.split(",")]
    calls = sum(4 if any(c.get("sentiment") for c in x.data["setups"]) else 3 for x in chosen) + len(chosen)
    print(f"Scenario suite: {len(chosen)} scenarios, about {calls} Claude calls on {s.claude_model} ({s.claude_effort} effort).")
    print(f"Estimated cost: roughly ${calls * EST_COST_PER_CALL:.2f} (actual cost is printed at the end).")
    print(f"Scenarios approved by owner: {'yes' if sc_mod.APPROVED else 'NO (a pass will not unlock deployment)'}")
    if not args.yes:
        print("Nothing sent. Re-run with --yes to run the suite.")
        return 0
    if not s.anthropic_api_key:
        print("ANTHROPIC_API_KEY is missing.", file=sys.stderr)
        return 2

    panel = ClaudePanel(s.anthropic_api_key, s.claude_model, s.claude_effort)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = datetime.now(timezone.utc)
    graded = await asyncio.gather(*(run_one(panel, x, sem) for x in chosen))
    summary = summarise(graded)

    marks = gate.PASS_MARKS
    full_run = len(chosen) == len(sc_mod.SCENARIOS)
    passed = (full_run
              and summary["errors"] == 0
              and summary["final_decision_accuracy"] >= marks["final_decision_accuracy"]
              and summary["specialist_accuracy"] >= marks["specialist_accuracy"]
              and summary["safety_violations"] <= marks["safety_violations"])
    line = (f"final decisions {summary['final_correct']}/{summary['final_total']} "
            f"({summary['final_decision_accuracy']:.0%}), specialists {summary['specialist_correct']}/"
            f"{summary['specialist_total']} ({summary['specialist_accuracy']:.0%}), "
            f"safety violations {summary['safety_violations']}, errors {summary['errors']}")
    u = panel.usage
    result = {
        "fingerprint": gate.fingerprint(s),
        "passed": passed,
        "scenarios_approved": sc_mod.APPROVED,
        "full_run": full_run,
        "summary": line,
        "scores": summary,
        "pass_marks": marks,
        "model": s.claude_model,
        "effort": s.claude_effort,
        "usage": {"calls": u.calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                  "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
                  "estimated_cost_usd": round(u.cost_usd, 2)},
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out_dir = gate.result_path(s).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if full_run:
        gate.result_path(s).write_text(json.dumps(result, indent=1))
    report = write_report(out_dir / "eval_report.html", result, graded, chosen)

    print()
    print(("PASSED" if passed else "NOT PASSED") + ": " + line)
    print(f"Claude calls {u.calls}, estimated cost ${u.cost_usd:.2f}. Report: {report}")
    if not full_run:
        print("Partial run: the deployment gate was not updated.")
    elif passed and not sc_mod.APPROVED:
        print("Scores pass, but the scenarios are not approved yet, so the desk will still refuse to start.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
