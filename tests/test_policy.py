"""Unit tests: no GPU, no AWS. A fake Decider client returns scripted signals."""
from __future__ import annotations

import pytest

from alarm_triage import CATEGORIES, PlantTriage, decision_message, load_all, make_alarm, make_policy
from decider_policy import DeciderUnavailable, PolicyGate

classify, route = make_policy()


def sig(d=0.1, c=0.1, n=0.1, m=0.1):
    return {"danger": d, "consequence": c, "noise": n, "maintenance": m}


class FakeClient:
    """Answers every noul question from a fixed signal dict and records the states it saw."""

    def __init__(self, signals: dict, fail: bool = False):
        self.signals, self.fail, self.calls = signals, fail, []

    def ask(self, state, questions):
        if self.fail:
            raise DeciderUnavailable("down")
        self.calls.append((state, set(questions)))
        return {"answers": {k: {"type": "noul", "noul": self.signals[k]} for k in questions}, "latency_ms": 12.0, "model": "fake"}


# ---------- router: the safety-relevant logic ----------

def test_danger_outranks_everything():
    assert classify(sig(d=0.8, c=0.9, n=0.9, m=0.9))[0] == "safety_critical"
    assert route("safety_critical", sig(d=0.8)) == "escalate"


def test_uncertain_danger_is_never_suppressed():
    cat, _ = classify(sig(d=0.4, n=0.95))
    assert cat == "nuisance"
    assert route(cat, sig(d=0.4, n=0.95)) == "review"


def test_confident_noise_and_maintenance_are_suppressed():
    assert route(*_cr(sig(n=0.9))) == "suppress"
    assert route(*_cr(sig(m=0.85))) == "suppress"


def test_weak_duplicate_goes_to_a_human():
    cat, _ = classify(sig(c=0.5))
    assert cat == "duplicate_of_active"
    assert route(cat, sig(c=0.5)) == "review"


def test_nothing_explains_it_away_means_actionable_and_escalated():
    assert route(*_cr(sig())) == "escalate"


def _cr(s):
    return classify(s)[0], s


# ---------- gate ----------

def test_gate_fails_toward_a_human_when_decider_is_down():
    gate = PolicyGate({"danger": "?"}, classify=lambda s: ("x", 1.0), route=lambda c, s: "suppress", client=FakeClient({}, fail=True))
    d = gate.decide("state")
    assert d.degraded and d.route == "review"


def test_scoped_signals_each_get_their_own_state():
    client = FakeClient(sig())
    profile = load_all()["process_plant"]
    triage = PlantTriage(profile, client=client)
    triage.decide(make_alarm(profile, "FI-301-LO", "r", 0, ts=1000.0))
    by_scope = {frozenset(q): s for s, q in client.calls}
    assert "ALREADY ACTIVE" not in by_scope[frozenset({"danger", "noise"})]  # danger never sees the cascade
    assert "OPERATOR LOG" in by_scope[frozenset({"maintenance"})]
    assert "ALREADY ACTIVE" in by_scope[frozenset({"consequence"})]


# ---------- profiles + contract ----------

@pytest.mark.parametrize("pid", sorted(load_all()))
def test_profiles_are_consistent(pid):
    p = load_all()[pid]
    assert set(p.decision["signals"]) == {"danger", "consequence", "noise", "maintenance"}
    labels = {ev["expected"] for s in p.scenarios for ev in s["events"]}
    assert labels <= CATEGORIES and "safety_critical" in labels


def test_connected_is_undirected():
    p = load_all()["process_plant"]
    # cooling-water pump P-301 feeds the condenser E-202, which the column C-201 also feeds
    assert "C-201" in p.connected("P-301")


def test_decision_message_contract():
    profile = load_all()["process_plant"]
    triage = PlantTriage(profile, client=FakeClient(sig(n=0.9)))
    alarm = make_alarm(profile, "LSH-305", "run1", 3, ts=1700000000.5, expected="nuisance")
    d, _ = triage.decide(alarm)
    msg = decision_message(alarm, d, "edge-test")
    assert msg["sk"] == "run1#1700000000500#run1-0003"
    assert msg["route"] == "suppress" and msg["category"] == "nuisance"
    for key in ("plant_id", "run_id", "tag", "signals", "latency_ms", "edge_id", "expected"):
        assert key in msg
