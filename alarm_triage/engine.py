"""Triage engine: one PolicyGate + one PlantState per plant. Used by the edge agent and the benchmark."""
from __future__ import annotations

import time

from decider_policy import DeciderClient, PolicyDecision, PolicyGate

from .profiles import Profile
from .router import make_policy
from .state import PlantState

# Which context each signal reads (see state.py). Profiles may override under decision.scopes.
SIGNAL_SCOPES = {"danger": "alarm", "noise": "alarm", "maintenance": "oplog", "consequence": "full"}


class PlantTriage:
    def __init__(self, profile: Profile, client: DeciderClient | None = None):
        self.profile = profile
        self.state = PlantState(profile)
        classify, route = make_policy(profile.routing)
        self.gate = PolicyGate(
            signals=profile.decision["signals"],
            classify=classify,
            route=route,
            client=client,
            scopes={**SIGNAL_SCOPES, **profile.decision.get("scopes", {})},
        )

    def decide(self, alarm: dict) -> tuple[PolicyDecision, dict[str, str]]:
        states = self.state.build(alarm)
        d = self.gate.decide(states)
        self.state.record(alarm, d.category if not d.degraded else "unclassified")
        return d, states


def make_alarm(profile: Profile, tag: str, run_id: str, seq: int, ts: float | None = None, expected: str | None = None) -> dict:
    a = profile.alarm_catalog[tag]
    alarm = {
        "plant_id": profile.profile_id,
        "run_id": run_id,
        "alarm_id": f"{run_id}-{seq:04d}",
        "ts": ts if ts is not None else time.time(),
        "tag": tag,
        "priority": a["priority"],
        "text": a["text"],
        "equipment": a["equipment"],
        "area": profile.equipment[a["equipment"]]["area"],
    }
    if expected:
        alarm["expected"] = expected  # ground truth for scoring; never shown to Decider
    return alarm


def decision_message(alarm: dict, d: PolicyDecision, edge_id: str) -> dict:
    """The edge -> core contract (topic plants/{plant_id}/decisions)."""
    return {
        **alarm,
        "sk": f"{alarm['run_id']}#{int(alarm['ts'] * 1000):013d}#{alarm['alarm_id']}",
        "category": d.category,
        "confidence": round(d.confidence, 4),
        "signals": {k: round(v, 4) for k, v in d.signals.items()},
        "route": d.route,
        "latency_ms": round(d.latency_ms, 1),
        "model": d.model,
        "degraded": d.degraded,
        "edge_id": edge_id,
    }
