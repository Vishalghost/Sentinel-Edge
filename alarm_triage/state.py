"""Builds the text states Decider reads for each alarm.

Scoped on purpose -- each signal sees only the context it needs:
  alarm  -> the new alarm and its equipment          (danger, noise)
  oplog  -> the new alarm + the operator log         (maintenance)
  full   -> alarm + active alarms + topology + log   (consequence)
The full scope is where Decider beats an if-statement: it reads the operator log and
how equipment is connected, which a priority-code rule cannot.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .profiles import Profile

WINDOW_S = 600  # alarms older than this no longer count as "active"
MAX_RECENT = 20
SCOPES = ("alarm", "oplog", "full")


@dataclass
class PlantState:
    profile: Profile
    recent: deque = field(default_factory=lambda: deque(maxlen=MAX_RECENT))  # (ts, alarm, category)
    oplog: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.recent.clear()
        self.oplog.clear()

    def add_oplog(self, note: str) -> None:
        self.oplog.append(note)

    def record(self, alarm: dict, category: str) -> None:
        self.recent.append((alarm.get("ts", time.time()), alarm, category))

    def _alarm_block(self, alarm: dict) -> list[str]:
        p = self.profile
        eq = alarm["equipment"]
        info = p.equipment.get(eq, {})
        lines = [
            f"Plant: {p.display_name} ({p.industry}). {p.description}",
            "",
            f"NEW ALARM: {alarm['tag']} [{alarm['priority']}] \"{alarm['text']}\"",
            f"  equipment {eq} ({info.get('type', '?')}) in {p.areas.get(info.get('area'), info.get('area'))}",
        ]
        if p.alarm_catalog.get(alarm["tag"], {}).get("chattering"):
            lines.append("  history: this alarm is known to chatter (repeats without a process change)")
        return lines

    def _oplog_block(self, alarm: dict) -> list[str]:
        eq = alarm["equipment"]
        mentions = [n for n in self.oplog if eq in n]
        lines = ["", "OPERATOR LOG: " + (" | ".join(self.oplog) if self.oplog else "no entries")]
        lines.append(f"  entries naming {eq}: " + (" | ".join(mentions) if mentions else "none"))
        return lines

    def build(self, alarm: dict) -> dict[str, str]:
        """Return {scope: state text} for one new alarm."""
        p = self.profile
        eq = alarm["equipment"]
        info = p.equipment.get(eq, {})
        now = alarm.get("ts", time.time())
        head = self._alarm_block(alarm)

        full = list(head)
        upstream = p.upstream_of(eq)
        downstream = info.get("feeds") or []
        if upstream:
            full.append("  fed by: " + ", ".join(f"{u} ({p.equipment[u]['type']})" for u in upstream))
        if downstream:
            full.append(f"  feeds: {', '.join(downstream)}")
        active = [(ts, a, c) for ts, a, c in self.recent if now - ts <= WINDOW_S]
        connected = p.connected(eq)
        full.append("")
        if active:
            full.append("ALREADY ACTIVE (oldest first):")
            for ts, a, c in active:
                link = ""
                if a["equipment"] == eq:
                    link = "  [same equipment]"
                elif a["equipment"] in connected:
                    link = f"  [connected equipment: {connected[a['equipment']]}]"
                full.append(f"  -{now - ts:4.0f}s {a['tag']} [{a['priority']}] \"{a['text']}\" -> {c}{link}")
        else:
            full.append("ALREADY ACTIVE: none")
        full += self._oplog_block(alarm)

        return {
            "alarm": "\n".join(head),
            "oplog": "\n".join(head + self._oplog_block(alarm)),
            "full": "\n".join(full),
        }
