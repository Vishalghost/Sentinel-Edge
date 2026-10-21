"""Alarm policy: plain Python turns Decider's yes/no signals into a category and a route.

Signals (asked in one Decider request): danger, consequence, noise, maintenance.
Costs are asymmetric -- a missed safety alarm is far worse than an extra one -- so
danger outranks everything and the "uncertain danger" band goes to a human, never
to the bin. Profiles may override any threshold under a `routing:` key.
"""
from __future__ import annotations

from decider_policy import Signals

DEFAULTS = {
    "danger": 0.50,          # >= this: safety_critical
    "danger_review": 0.35,   # in [danger_review, danger): never suppress, show a human
    "noise": 0.50,           # >= this (and >= consequence): nuisance
    "maintenance": 0.50,     # >= this: nuisance (equipment is out of service per operator log)
    "consequence": 0.45,     # >= this: duplicate_of_active
    "suppress_noise": 0.60,  # hide a nuisance only when this sure
    "suppress_consequence": 0.55,  # fold a duplicate into its incident only when this sure
}


def make_policy(overrides: dict | None = None):
    cfg = {**DEFAULTS, **(overrides or {})}

    def classify(s: Signals) -> tuple[str, float]:
        if s["danger"] >= cfg["danger"]:
            return "safety_critical", s["danger"]
        quiet = max(s["noise"], s.get("maintenance", 0.0))
        if s.get("maintenance", 0.0) >= cfg["maintenance"] or (s["noise"] >= cfg["noise"] and s["noise"] >= s["consequence"]):
            return "nuisance", quiet
        if s["consequence"] >= cfg["consequence"]:
            return "duplicate_of_active", s["consequence"]
        # nothing explains it away: a real, new problem
        return "actionable", 1.0 - max(s["noise"], s.get("maintenance", 0.0), s["consequence"])

    def route(category: str, s: Signals) -> str:
        if category in ("safety_critical", "actionable"):
            return "escalate"
        if s["danger"] >= cfg["danger_review"]:
            return "review"
        if category == "nuisance":
            return "suppress" if max(s["noise"], s.get("maintenance", 0.0)) >= cfg["suppress_noise"] else "review"
        if category == "duplicate_of_active":
            return "suppress" if s["consequence"] >= cfg["suppress_consequence"] else "review"
        return "review"

    return classify, route
