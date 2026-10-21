"""Historical experiment (kept for the write-up): one 4-way choice vs yes/no signals in one request.

Note: written against an early engine API (single state string); see README for the final results.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alarm_triage import PlantState, load_all, make_alarm  # noqa: E402
from decider_policy import DeciderClient  # noqa: E402

client = DeciderClient()
NOULS = {
    "danger": {
        "type": "noul",
        "instructions": "Does the NEW alarm itself signal an immediate hazard to people or equipment, such as a relief valve lifting, flammable gas, fire or overpressure?",
    },
    "consequence": {
        "type": "noul",
        "instructions": "Is the NEW alarm most likely a downstream consequence of an alarm that is ALREADY ACTIVE on connected equipment?",
    },
    "noise": {
        "type": "noul",
        "instructions": "Is the NEW alarm chattering, or explained by maintenance recorded in the operator log, so that no operator action is needed?",
    },
}


def combine(p):
    if p["danger"] >= 0.5:
        return "safety_critical"
    if p["noise"] >= 0.5 and p["noise"] >= p["consequence"]:
        return "nuisance"
    if p["consequence"] >= 0.5:
        return "duplicate_of_active"
    return "actionable"


for prof in load_all().values():
    for sc in prof.scenarios:
        st = PlantState(prof)
        for n in sc.get("oplog", []):
            st.add_oplog(n)
        t0 = time.time()
        hit = 0
        print(f"\n== {prof.profile_id}/{sc['id']}")
        for i, ev in enumerate(sc["events"]):
            a = make_alarm(prof, ev["tag"], "x", i, ts=t0 + ev["t"])
            s = st.build(a)
            r = client.ask(s, NOULS)
            p = {k: v["noul"] for k, v in r["answers"].items()}
            cat = combine(p)
            st.record(a, cat)
            ok = cat == ev["expected"]
            hit += ok
            print(f"{ev['tag']:<14} d={p['danger']:.2f} c={p['consequence']:.2f} n={p['noise']:.2f} -> {cat:<20} exp={ev['expected']:<20} {'OK' if ok else 'MISS'} {r['latency_ms']:.0f}ms")
        print(f"   {hit}/{len(sc['events'])}")
