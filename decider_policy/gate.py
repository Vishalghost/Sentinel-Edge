"""PolicyGate: classify an incoming request with Decider 2B *before* an agent acts on it.

Pattern: ask Decider several focused yes/no *signals* about one state in a single
request (the state is encoded once), then let plain Python turn the signals into
a category and the category into a route. The model only measures; code decides.

Two layers, reuse either one:

* ``PolicyGate`` -- framework-free: ``decide(state) -> PolicyDecision``.
* ``PolicyGateIntervention`` -- a Strands ``InterventionHandler``. On
  ``before_invocation`` it runs the gate and returns ``Deny`` for routes you mark
  as blocked, so the LLM is never called for them.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .client import DeciderClient, DeciderUnavailable

Signals = dict[str, float]  # signal name -> P(yes)
ClassifyFn = Callable[[Signals], tuple[str, float]]  # -> (category, confidence)
RouteFn = Callable[[str, Signals], str]  # (category, signals) -> route


@dataclass(frozen=True)
class PolicyDecision:
    category: str
    confidence: float
    signals: Signals
    route: str
    latency_ms: float
    model: str
    degraded: bool = False  # True when Decider was unavailable and the fallback route was used

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyGate:
    def __init__(
        self,
        signals: dict[str, str],
        classify: ClassifyFn,
        route: RouteFn,
        client: DeciderClient | None = None,
        fallback_route: str = "review",
        criteria: dict[str, dict[str, str]] | None = None,
        scopes: dict[str, str] | None = None,
    ):
        """signals: name -> yes/no question. criteria: optional name -> {"true": ..., "false": ...}.

        scopes: optional name -> scope. Signals sharing a scope go in one request; ``decide``
        then takes {scope: state}. Give each signal only the context it needs -- a small
        model asked "is THIS alarm dangerous?" while reading a whole cascade tends to answer
        "is the PLANT in danger?" instead.
        """
        if not signals:
            raise ValueError("a policy needs at least one signal")
        self.scopes = scopes or {name: "default" for name in signals}
        self.requests: dict[str, dict[str, dict]] = {}
        for name, q in signals.items():
            question = {"type": "noul", "instructions": q}
            if criteria and name in criteria:
                question["criteria"] = criteria[name]
            self.requests.setdefault(self.scopes.get(name, "default"), {})[name] = question
        self.classify = classify
        self.route = route
        self.client = client or DeciderClient()
        # Fail toward a human, not toward silence: if Decider is down, nothing gets suppressed.
        self.fallback_route = fallback_route

    def decide(self, state: Any) -> PolicyDecision:
        """state: one state for all signals, or {scope: state} when scopes are used."""
        states = state if isinstance(state, dict) and set(self.requests) <= set(state) else {s: state for s in self.requests}
        t0 = time.perf_counter()
        signals: Signals = {}
        latency, model = 0.0, ""
        try:
            for scope, questions in self.requests.items():
                resp = self.client.ask(states[scope], questions)
                signals.update({k: float(v["noul"]) for k, v in resp["answers"].items()})
                latency += float(resp.get("latency_ms") or resp.get("_wall_ms") or 0.0)  # excludes rate-limit waits
                model = resp.get("model", model)
        except DeciderUnavailable:
            return PolicyDecision("unclassified", 0.0, {}, self.fallback_route, 0.0, "none", degraded=True)
        category, confidence = self.classify(signals)
        latency = latency or (time.perf_counter() - t0) * 1000
        return PolicyDecision(category, confidence, signals, self.route(category, signals), latency, model)


try:  # the Strands wiring is optional so the gate also works outside Strands
    from strands.interventions import Deny, InterventionHandler, Proceed

    def _last_user_text(event) -> str:
        for msg in reversed(event.messages or []):
            if msg.get("role") == "user":
                return " ".join(c.get("text", "") for c in msg.get("content", []) if "text" in c)
        return ""

    class PolicyGateIntervention(InterventionHandler):
        """Put a PolicyGate in a Strands agent's path.

        agent = Agent(interventions=[PolicyGateIntervention(gate, deny_routes={"suppress"})])
        agent("...")   # never reaches the model when the gate routes to "suppress"

        Pass the Decider state explicitly with ``agent(prompt, policy_state=...)`` or let
        the handler use the last user message. The decision is stored in
        ``invocation_state["policy_decision"]`` and on ``self.last_decision``.
        """

        name = "decider-policy-gate"

        def __init__(
            self,
            gate: PolicyGate,
            deny_routes: set[str] | frozenset[str] = frozenset({"suppress"}),
            state_from_event: Callable[[Any], Any] = _last_user_text,
        ):
            self.gate = gate
            self.deny_routes = set(deny_routes)
            self.state_from_event = state_from_event
            self.last_decision: PolicyDecision | None = None

        def before_invocation(self, event, **kwargs):
            state = event.invocation_state.get("policy_state") or self.state_from_event(event)
            d = self.gate.decide(state)
            self.last_decision = d
            event.invocation_state["policy_decision"] = d
            if d.route in self.deny_routes:
                return Deny(reason=f"policy gate: {d.category} ({d.confidence:.2f}) -> {d.route}")
            return Proceed(reason=f"{d.category} ({d.confidence:.2f}) -> {d.route}")

except ImportError:  # pragma: no cover - strands not installed
    PolicyGateIntervention = None  # type: ignore[assignment,misc]
