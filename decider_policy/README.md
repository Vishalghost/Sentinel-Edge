# Reusable Strands Decider 2B decision pattern: PolicyGate (`#policy`)

| | |
|---|---|
| **Pattern** | Policy: classify an incoming request before the agent acts on it |
| **Typed questions** | N `noul` (yes/no) signals, grouped by context scope, one Decider request per scope |
| **Option sets** | categories `safety_critical / actionable / duplicate_of_active / nuisance` → routes `escalate / review / suppress` |
| **Wiring** | `PolicyGateIntervention`, a Strands `InterventionHandler` (`before_invocation` → `Deny` skips the LLM) |
| **Low confidence** | anything uncertain routes to `review` (a human), never `suppress`; Decider unavailable → `review` |
| **License** | Apache-2.0 |

**Classify an incoming request with Strands Decider 2B *before* your agent acts on it, and skip the LLM entirely when it doesn't need one.**

The pattern in one line: ask Decider several focused **yes/no signals** about one state, let **plain Python** combine them into a category and a route, and put that gate in the agent's path with a Strands `InterventionHandler`.

```
request ──► PolicyGate ──► Decider 2B (N yes/no signals, ~150-500 ms, local)
                │
                ├─ classify(signals) -> category      (your code: precedence, thresholds)
                ├─ route(category, signals) -> route  (your code: asymmetric costs)
                ▼
     Deny ("suppress")  → the agent never calls the model
     Proceed            → the agent runs on Amazon Bedrock as usual
```

## 1. The typed questions

Signals are `noul` (yes/no) questions. Each one asks about **one property**:

```python
SIGNALS = {
    "danger":      "Does the NEW alarm itself signal an immediate hazard to people or equipment ...?",
    "consequence": "Is the NEW alarm most likely a downstream consequence of an alarm that is ALREADY ACTIVE ...?",
    "noise":       "Is the NEW alarm a chattering or nuisance alarm that repeats without any real process change?",
    "maintenance": "According to the operator log, is the equipment of the NEW alarm isolated or under maintenance right now?",
}
CATEGORIES = ["safety_critical", "actionable", "duplicate_of_active", "nuisance"]
ROUTES     = ["escalate", "review", "suppress"]
```

## 2. The wiring

```python
from strands import Agent
from decider_policy import PolicyGate, PolicyGateIntervention

def classify(s):                       # precedence is policy: danger outranks everything
    if s["danger"] >= 0.50:      return "safety_critical", s["danger"]
    if s["maintenance"] >= 0.50 or s["noise"] >= 0.50: return "nuisance", max(s["noise"], s["maintenance"])
    if s["consequence"] >= 0.45: return "duplicate_of_active", s["consequence"]
    return "actionable", 1 - max(s.values())

def route(category, s):                # asymmetric: uncertain danger goes to a human, never the bin
    if category in ("safety_critical", "actionable"): return "escalate"
    if s["danger"] >= 0.35:            return "review"
    return "suppress" if max(s["noise"], s["maintenance"], s["consequence"]) >= 0.55 else "review"

gate = PolicyGate(
    SIGNALS, classify, route,
    scopes={"danger": "alarm", "noise": "alarm", "maintenance": "oplog", "consequence": "full"},
)
policy = PolicyGateIntervention(gate, deny_routes={"suppress", "review"})
agent = Agent(model=bedrock_model, interventions=[policy])

result = agent("Write the operator note for this alarm",
               invocation_state={"policy_state": {"alarm": ..., "oplog": ..., "full": ...}})
decision = policy.last_decision   # category, confidence, signals, route, latency_ms
```

Without Strands, call `gate.decide(state)` directly and branch on `decision.route`.

## 3. How to use it in your agent

1. **Write one yes/no signal per property, not one multi-way choice.** On our alarm data, a single 4-way `choice` question scored **45%**; the same model with four `noul` signals scored **85-90%**. Properties that can be true together (an alarm can be dangerous *and* a consequence) should not compete in one softmax.
2. **Scope the state per signal.** Give each signal only the context it needs (`scopes=`). A 2B model asked "is THIS alarm dangerous?" while reading a whole cascade answers "is the PLANT in danger?". Scoping lifted held-out accuracy from **57-60% to 79-80%**. Signals that share a scope go in **one request**: the state is encoded once and each extra question is cheap.
3. **Keep decisions in code.** Decider measures; your `classify`/`route` functions decide. Thresholds are reviewable, testable and tunable per deployment (here: per plant profile YAML).
4. **Make failure safe.** `PolicyGate(fallback_route="review")` is the default: if Decider is down, nothing is suppressed; requests go to a human.
5. **Tune on one set, report on another.** We tuned on one plant and quote held-out plants.

## Files

- [`client.py`](client.py): stdlib-only HTTP client for `strands-decider serve` (`ask`, `choice`, `noul`)
- [`gate.py`](gate.py): `PolicyGate` (framework-free) + `PolicyGateIntervention` (Strands `before_invocation`)

Run a Decider server: `strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099` (set `STRANDS_DECIDER_URL` if elsewhere).
