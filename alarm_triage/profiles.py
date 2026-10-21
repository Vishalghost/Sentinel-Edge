"""Plant profiles: all industry knowledge lives in profiles/*.yaml, none in code."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CATEGORIES = {"safety_critical", "actionable", "duplicate_of_active", "nuisance"}
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass(frozen=True)
class Profile:
    profile_id: str
    display_name: str
    industry: str
    description: str
    areas: dict
    equipment: dict
    alarm_catalog: dict
    decision: dict
    scenarios: list
    routing: dict

    def scenario(self, scenario_id: str) -> dict:
        for s in self.scenarios:
            if s["id"] == scenario_id:
                return s
        raise KeyError(f"{self.profile_id} has no scenario {scenario_id!r}")

    def upstream_of(self, tag: str) -> list[str]:
        return [t for t, e in self.equipment.items() if tag in (e.get("feeds") or [])]

    def connected(self, tag: str, max_hops: int = 2) -> dict[str, str]:
        """Equipment within max_hops in either flow direction -> human-readable path.

        Undirected on purpose: a cooling failure at a condenser pushes pressure back
        *upstream* into the column, so following only the `feeds` direction misses it.
        """
        adj: dict[str, set[str]] = {t: set() for t in self.equipment}
        for t, e in self.equipment.items():
            for d in e.get("feeds") or []:
                adj.setdefault(t, set()).add(d)
                adj.setdefault(d, set()).add(t)
        paths, frontier = {tag: tag}, [tag]
        for _ in range(max_hops):
            nxt = []
            for node in frontier:
                for n in adj.get(node, ()):
                    if n not in paths:
                        paths[n] = f"{paths[node]} - {n}"
                        nxt.append(n)
            frontier = nxt
        paths.pop(tag)
        return paths

    def summary(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "industry": self.industry,
            "scenarios": [{"id": s["id"], "title": s["title"], "events": len(s["events"])} for s in self.scenarios],
        }


def load_profile(path: str | Path) -> Profile:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for tag, a in raw["alarm_catalog"].items():
        if a["equipment"] not in raw["equipment"]:
            raise ValueError(f"{path}: alarm {tag} references unknown equipment {a['equipment']}")
    options = CATEGORIES
    if not raw["decision"].get("signals"):
        raise ValueError(f"{path}: decision.signals is required")
    for s in raw["scenarios"]:
        for ev in s["events"]:
            if ev["tag"] not in raw["alarm_catalog"]:
                raise ValueError(f"{path}: scenario {s['id']} uses unknown alarm {ev['tag']}")
            if ev.get("expected") and ev["expected"] not in options:
                raise ValueError(f"{path}: scenario {s['id']} label {ev['expected']} is not an option")
    return Profile(
        profile_id=raw["profile_id"],
        display_name=raw["display_name"],
        industry=raw["industry"],
        description=" ".join(raw.get("description", "").split()),
        areas=raw["areas"],
        equipment=raw["equipment"],
        alarm_catalog=raw["alarm_catalog"],
        decision=raw["decision"],
        scenarios=raw["scenarios"],
        routing=raw.get("routing", {}),
    )


def load_all(directory: str | Path = PROFILES_DIR) -> dict[str, Profile]:
    profiles = [load_profile(p) for p in sorted(Path(directory).glob("*.yaml"))]
    return {p.profile_id: p for p in profiles}
