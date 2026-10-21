"""Run a profile scenario through Decider locally -- no AWS. Useful for tuning and as a demo fallback.

    python -m edge.local_run process_plant cooling_failure_cascade [--show-state]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alarm_triage import PlantTriage, load_all, make_alarm  # noqa: E402

COLORS = {"escalate": "\033[91m", "review": "\033[93m", "suppress": "\033[90m"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--show-state", action="store_true")
    args = ap.parse_args()

    profile = load_all()[args.profile]
    scenarios = [profile.scenario(args.scenario)] if args.scenario else profile.scenarios
    hits = total = 0
    lat = []
    for sc in scenarios:
        triage = PlantTriage(profile)
        for note in sc.get("oplog", []):
            triage.state.add_oplog(note)
        print(f"\n=== {profile.profile_id} / {sc['id']}: {sc['title']}")
        t0 = time.time()
        for i, ev in enumerate(sc["events"]):
            alarm = make_alarm(profile, ev["tag"], "local", i, ts=t0 + ev["t"], expected=ev.get("expected"))
            d, state = triage.decide(alarm)
            if args.show_state:
                print("-" * 60 + "\n" + state)
            ok = d.category == ev.get("expected")
            hits += ok
            total += 1
            lat.append(d.latency_ms)
            c = COLORS.get(d.route, "")
            sig = " ".join(f"{k[0]}={v:.2f}" for k, v in d.signals.items())
            print(f"{c}{ev['tag']:<14} {sig}  {d.category:<20} -> {d.route:<8}\033[0m "
                  f"{d.latency_ms:6.0f} ms  expected={ev.get('expected'):<20} {'OK' if ok else 'MISS'}")
    print(f"\naccuracy {hits}/{total} = {hits / total:.0%}   p50 {statistics.median(lat):.0f} ms   max {max(lat):.0f} ms")


if __name__ == "__main__":
    main()
