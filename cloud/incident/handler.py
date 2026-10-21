"""Incident grouper Lambda (SQS trigger).

Only escalated alarms arrive here. The SQS batching window (a few seconds) already
groups a cascade into one batch; alarms of a run that land while an incident is
still fresh are attached to it instead of opening a new one.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict

from agent import rca_agent, store

log = logging.getLogger()
log.setLevel(logging.INFO)
ATTACH_WINDOW_S = 120


def _open_incident(plant_id: str, run_id: str) -> dict | None:
    fresh = [i for i in store.incidents_for_run(plant_id, run_id) if time.time() - i.get("updated", 0) < ATTACH_WINDOW_S]
    return max(fresh, key=lambda i: i["updated"]) if fresh else None


def _handle_group(plant_id: str, run_id: str, alarms: list[dict]) -> None:
    inc = _open_incident(plant_id, run_id)
    now = time.time()
    if inc is None:
        inc_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
        inc = {"plant_id": plant_id, "sk": f"INC#{run_id}#{inc_id}", "incident_id": inc_id, "run_id": run_id, "created": now, "alarms": []}
    inc["alarms"] = inc["alarms"] + [{"alarm_id": a["alarm_id"], "tag": a["tag"], "category": a["category"]} for a in alarms]
    inc.update(status="analysing", updated=now)
    store.put_incident(inc)  # dashboard shows the card immediately

    t0 = time.time()
    try:
        rca, usage = rca_agent.root_cause(plant_id, run_id, alarms)
        inc.update(rca=rca, usage=usage, status="open")
    except Exception as e:  # Bedrock throttling / access: keep the incident, show the error, don't retry the batch
        log.exception("RCA failed")
        inc.update(status="rca_failed", rca_error=str(e)[:500])
    inc.update(rca_ms=round((time.time() - t0) * 1000), updated=time.time())
    store.put_incident(inc)
    log.info("incident %s: %d alarms, %s in %d ms", inc["incident_id"], len(alarms), inc["status"], inc["rca_ms"])


def handler(event, _context):
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
    failures = []
    for rec in event["Records"]:
        try:
            a = json.loads(rec["body"])
            groups[(a["plant_id"], a["run_id"])].append((rec["messageId"], a))
        except (KeyError, json.JSONDecodeError):
            log.error("malformed message %s", rec.get("messageId"))
            failures.append({"itemIdentifier": rec["messageId"]})  # -> retries, then DLQ
    for (plant_id, run_id), items in groups.items():
        try:
            _handle_group(plant_id, run_id, sorted((a for _, a in items), key=lambda a: a["ts"]))
        except Exception:
            log.exception("group %s/%s failed", plant_id, run_id)
            failures += [{"itemIdentifier": mid} for mid, _ in items]
    return {"batchItemFailures": failures}
