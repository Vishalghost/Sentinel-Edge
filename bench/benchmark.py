"""Benchmark: Decider-gated Strands agent vs. an LLM-only agent, on every labelled scenario alarm.

  Arm A  llm_only  -- Claude (Bedrock) classifies every alarm (structured output).
  Arm B  gated     -- the same kind of Strands agent, with PolicyGateIntervention in its path:
                      Decider classifies; the LLM runs only for escalated alarms (to write an
                      operator note). Suppressed / review alarms never reach the model.

Needs a running Decider (STRANDS_DECIDER_URL) and Bedrock credentials.
    python bench/benchmark.py [--price-in 3.0 --price-out 15.0]
Writes bench/results/latest.json and bench/results/RESULTS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field  # noqa: E402
from strands import Agent  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402

from alarm_triage import PlantState, PlantTriage, load_all, make_alarm  # noqa: E402
from decider_policy import PolicyGateIntervention  # noqa: E402

OUT = Path(__file__).resolve().parent / "results"
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
REAL = {"safety_critical", "actionable"}

CATEGORY_HELP = (
    "Categories:\n"
    "- safety_critical: the alarm itself signals immediate danger to people, product safety or equipment\n"
    "- actionable: a real, new process problem an operator must respond to\n"
    "- duplicate_of_active: a downstream consequence of an alarm that is already active\n"
    "- nuisance: chattering, or on equipment under maintenance per the operator log; no action needed"
)


class Classification(BaseModel):
    category: Literal["safety_critical", "actionable", "duplicate_of_active", "nuisance"]
    reason: str = Field(description="one short sentence")


def snapshot(agent) -> tuple[int, int]:
    # accumulated_usage grows over the agent's lifetime: measure per call as a delta
    acc = agent.event_loop_metrics.accumulated_usage
    return acc.get("inputTokens", 0), acc.get("outputTokens", 0)


def delta(before: tuple[int, int], agent) -> tuple[int, int]:
    after = snapshot(agent)
    return after[0] - before[0], after[1] - before[1]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def run_llm_only(model: BedrockModel) -> list[dict]:
    rows = []
    agent = Agent(model=model, callback_handler=None,
                  system_prompt="You triage industrial alarms for a control room. " + CATEGORY_HELP)
    for profile in load_all().values():
        for sc in profile.scenarios:
            state = PlantState(profile)
            for note in sc.get("oplog", []):
                state.add_oplog(note)
            t0 = time.time()
            for i, ev in enumerate(sc["events"]):
                alarm = make_alarm(profile, ev["tag"], "bench", i, ts=t0 + ev["t"])
                text = state.build(alarm)["full"]
                agent.messages = []
                before = snapshot(agent)
                s = time.perf_counter()
                res = agent(f"Classify the NEW alarm.\n\n{text}", structured_output_model=Classification)
                ms = (time.perf_counter() - s) * 1000
                cat = res.structured_output.category
                state.record(alarm, cat)
                tin, tout = delta(before, agent)
                rows.append(dict(profile=profile.profile_id, scenario=sc["id"], tag=ev["tag"], expected=ev["expected"],
                                 category=cat, route="escalate" if cat in REAL else "suppress",
                                 decision_ms=ms, llm_calls=1, tokens_in=tin, tokens_out=tout))
                print(f"  A {profile.profile_id:<18} {ev['tag']:<14} {cat:<20} exp={ev['expected']:<20} {ms:6.0f} ms")
    return rows


def run_gated(model: BedrockModel) -> list[dict]:
    rows = []
    for profile in load_all().values():
        triage = PlantTriage(profile)
        gate = PolicyGateIntervention(triage.gate, deny_routes={"suppress", "review"})
        agent = Agent(model=model, callback_handler=None, interventions=[gate],
                      system_prompt="You write one-sentence notes for control-room operators about escalated alarms.")
        for sc in profile.scenarios:
            triage.state.reset()
            for note in sc.get("oplog", []):
                triage.state.add_oplog(note)
            t0 = time.time()
            for i, ev in enumerate(sc["events"]):
                alarm = make_alarm(profile, ev["tag"], "bench", i, ts=t0 + ev["t"])
                states = triage.state.build(alarm)
                agent.messages = []
                before = snapshot(agent)
                res = agent(f"Write the operator note for this escalated alarm:\n{states['full']}",
                            invocation_state={"policy_state": states})
                d = gate.last_decision
                triage.state.record(alarm, d.category)
                called = d.route not in gate.deny_routes
                tin, tout = delta(before, agent)
                rows.append(dict(profile=profile.profile_id, scenario=sc["id"], tag=ev["tag"], expected=ev["expected"],
                                 category=d.category, route=d.route, decision_ms=d.latency_ms, signals=d.signals,
                                 llm_calls=int(called), tokens_in=tin, tokens_out=tout))
                print(f"  B {profile.profile_id:<18} {ev['tag']:<14} {d.category:<20} -> {d.route:<8} exp={ev['expected']:<20} {d.latency_ms:6.0f} ms")
    return rows


def summarise(rows: list[dict], price_in: float, price_out: float) -> dict:
    n = len(rows)
    real = [r for r in rows if r["expected"] in REAL]
    safety = [r for r in rows if r["expected"] == "safety_critical"]
    tin, tout = sum(r["tokens_in"] for r in rows), sum(r["tokens_out"] for r in rows)
    cost = tin / 1e6 * price_in + tout / 1e6 * price_out
    lat = [r["decision_ms"] for r in rows]
    by_profile = {}
    for pid in sorted({r["profile"] for r in rows}):
        pr = [r for r in rows if r["profile"] == pid]
        by_profile[pid] = round(sum(r["category"] == r["expected"] for r in pr) / len(pr), 3)
    return {
        "alarms": n,
        "category_accuracy": round(sum(r["category"] == r["expected"] for r in rows) / n, 3),
        "accuracy_by_profile": by_profile,
        "safety_recall_category": f"{sum(r['category'] == 'safety_critical' for r in safety)}/{len(safety)}",
        "safety_surfaced": f"{sum(r['route'] != 'suppress' for r in safety)}/{len(safety)}",
        "real_alarms_hidden": sum(r["route"] == "suppress" for r in real),
        "operator_load_cut": round(sum(r["route"] == "suppress" for r in rows) / n, 3),
        "decision_ms_p50": round(statistics.median(lat)),
        "decision_ms_p95": round(pct(lat, 0.95)),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "tokens_in": tin,
        "tokens_out": tout,
        "llm_cost_usd": round(cost, 4),
        "llm_cost_per_1k_alarms_usd": round(cost / n * 1000, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-in", type=float, default=3.0, help="USD per 1M input tokens (check Bedrock pricing)")
    ap.add_argument("--price-out", type=float, default=15.0, help="USD per 1M output tokens")
    args = ap.parse_args()
    model = BedrockModel(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION", "us-east-1"), temperature=0.0)

    print("Arm B: Decider-gated agent")
    gated = run_gated(model)
    print("Arm A: LLM-only agent")
    llm = run_llm_only(model)

    result = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "model_id": MODEL_ID,
        "price_assumption_usd_per_mtok": {"input": args.price_in, "output": args.price_out},
        "decider_hardware": os.environ.get("EDGE_GPU", "laptop RTX 4050 (WSL2)"),
        "gated": summarise(gated, args.price_in, args.price_out),
        "llm_only": summarise(llm, args.price_in, args.price_out),
        "rows": {"gated": gated, "llm_only": llm},
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "latest.json").write_text(json.dumps(result, indent=2))
    g, a = result["gated"], result["llm_only"]
    md = ["| metric | Decider-gated agent | LLM-only agent |", "|---|---|---|"]
    for k in ("alarms", "category_accuracy", "accuracy_by_profile", "safety_recall_category", "safety_surfaced",
              "real_alarms_hidden", "operator_load_cut", "decision_ms_p50", "decision_ms_p95", "llm_calls",
              "tokens_in", "tokens_out", "llm_cost_usd", "llm_cost_per_1k_alarms_usd"):
        md.append(f"| {k} | {g[k]} | {a[k]} |")
    header = (f"# Benchmark results ({result['generated']})\n\nLLM: `{MODEL_ID}` on Amazon Bedrock. Decider: Strands Decider 2B on "
              f"{result['decider_hardware']}. Cost uses an assumed ${args.price_in}/${args.price_out} per 1M input/output tokens; "
              "token counts are measured. Decider cost is $0 per call on owned hardware (electricity not counted).\n\n")
    (OUT / "RESULTS.md").write_text(header + "\n".join(md) + "\n")
    print("\n" + "\n".join(md))


if __name__ == "__main__":
    main()
