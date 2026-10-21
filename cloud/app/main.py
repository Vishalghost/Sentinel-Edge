"""Control-plane app (ECS Fargate): dashboard, plant simulator, operator chat.

The simulator publishes alarms to AWS IoT Core exactly like a plant gateway would;
it never talks to the edge directly. The dashboard polls /api/state, which reads
what the IoT Rules and the incident Lambda wrote to DynamoDB.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import boto3
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import rca_agent, store
from alarm_triage import load_all, make_alarm

STATIC = Path(__file__).parent / "static"
PROFILES = load_all()
PASSCODE = os.environ.get("DEMO_PASSCODE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
_iot = boto3.client("iot-data", region_name=REGION, endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}") if os.environ.get("IOT_ENDPOINT") else None
_s3 = boto3.client("s3", region_name=REGION)

app = FastAPI(title="Sentinel Edge")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_running: dict[str, asyncio.Task] = {}
_hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
DAILY_CAP = {"run": int(os.environ.get("DAILY_RUN_CAP", "200")), "chat": int(os.environ.get("DAILY_CHAT_CAP", "300")),
             "cloud_run": int(os.environ.get("CLOUD_DAILY_RUN_CAP", "8"))}
MONTHLY_ANSWERS = int(os.environ.get("DECIDER_MONTHLY_ANSWERS", "1800"))  # keep ~200 of the 2,000 free for testing
EDGE_FRESH_S = 20


def _guard(passcode: str | None, request: Request, bucket: str, per_hour: int) -> None:
    """Public URL + an LLM behind it = a cost risk. Optional passcode and a per-IP rate limit."""
    if PASSCODE and passcode != PASSCODE:
        raise HTTPException(401, "passcode required")
    key = f"{bucket}:{request.client.host if request.client else '?'}"
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= per_hour:
        raise HTTPException(429, "rate limit reached, try again later")
    day = _hits[f"{bucket}:__all__"]  # global cap: per-IP limits alone are bypassed by rotating IPs
    while day and now - day[0] > 86400:
        day.popleft()
    if len(day) >= DAILY_CAP[bucket]:
        raise HTTPException(429, "daily demo budget reached, try again tomorrow")
    q.append(now)
    day.append(now)


def _publish(topic: str, payload: dict) -> None:
    if _iot is None:
        raise HTTPException(500, "IOT_ENDPOINT is not configured")
    _iot.publish(topic=topic, qos=1, payload=json.dumps(payload).encode())


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/profiles")
def profiles():
    return {"profiles": [p.summary() for p in PROFILES.values()], "passcode_required": bool(PASSCODE)}


class RunRequest(BaseModel):
    plant_id: str
    scenario_id: str
    speed: float = 1.0
    edge: str = "auto"  # "auto" | an edge_id


def _online_edges() -> list[dict]:
    now = time.time()
    return [e for e in store.edge_status() if e.get("online") and now - e.get("ts", 0) < EDGE_FRESH_S]


def pick_edge(edges: list[dict], wanted: str = "auto") -> dict | None:
    """Exactly one owner per run (no split-brain). Auto prefers the free local GPU over the metered API."""
    if wanted != "auto":
        return next((e for e in edges if e["edge_id"] == wanted), None)
    ranked = sorted(edges, key=lambda e: (not e.get("decider_ok", True), e.get("kind") != "gpu"))  # working first, then free
    return ranked[0] if ranked else None


async def _simulate(plant_id: str, run_id: str, scenario: dict, speed: float, edge_id: str) -> None:
    profile = PROFILES[plant_id]
    t0 = time.time()
    for i, ev in enumerate(scenario["events"]):
        await asyncio.sleep(max(0.0, t0 + ev["t"] / speed - time.time()))
        alarm = make_alarm(profile, ev["tag"], run_id, i, expected=ev.get("expected"))
        alarm["edge_target"] = edge_id  # only this edge decides; any other edge ignores the alarm
        await asyncio.to_thread(_publish, f"plants/{plant_id}/alarms", alarm)
    store.put_run(plant_id, run_id, {"run_id": run_id, "scenario_id": scenario["id"], "title": scenario["title"], "started": t0,
                                     "finished": time.time(), "events": len(scenario["events"]), "edge_id": edge_id})


@app.post("/api/runs")
async def start_run(req: RunRequest, request: Request, x_demo_passcode: str | None = Header(default=None)):
    _guard(x_demo_passcode, request, "run", per_hour=30)
    profile = PROFILES.get(req.plant_id)
    if profile is None:
        raise HTTPException(404, "unknown plant")
    try:
        scenario = profile.scenario(req.scenario_id)
    except KeyError:
        raise HTTPException(404, "unknown scenario")
    if (t := _running.get(req.plant_id)) and not t.done():
        raise HTTPException(409, "a run is already in progress for this plant")
    edge = pick_edge(_online_edges(), req.edge)
    if edge is None:
        raise HTTPException(503, "no edge is online right now; showing the most recent recorded run instead")
    if edge.get("kind") == "api":  # metered: cap per day and reserve the month's answers up front
        _guard(x_demo_passcode, request, "cloud_run", per_hour=4)
        answers = len(scenario["events"]) * len(profile.decision["signals"])
        if not store.reserve_answers(answers, MONTHLY_ANSWERS):
            raise HTTPException(429, "the live Decider API budget for this month is used up; showing the most recent recorded run")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    store.put_run(req.plant_id, run_id, {"run_id": run_id, "scenario_id": scenario["id"], "title": scenario["title"], "started": time.time(),
                                         "events": len(scenario["events"]), "edge_id": edge["edge_id"], "edge_kind": edge.get("kind", "gpu")})
    _publish(f"plants/{req.plant_id}/control", {"action": "reset", "run_id": run_id})
    for note in scenario.get("oplog", []):
        store.put_oplog(req.plant_id, run_id, note)
        _publish(f"plants/{req.plant_id}/oplog", {"run_id": run_id, "note": note})
    _running[req.plant_id] = asyncio.create_task(_simulate(req.plant_id, run_id, scenario, max(0.25, min(req.speed, 4.0)), edge["edge_id"]))
    return {"run_id": run_id, "scenario": scenario["title"], "edge_id": edge["edge_id"], "edge_kind": edge.get("kind", "gpu")}


@app.get("/api/state")
def state(plant_id: str, run_id: str | None = None, after: str | None = None):
    run = store.latest_run(plant_id) if not run_id else {"run_id": run_id}
    budget = {"used": store.answers_used(), "limit": MONTHLY_ANSWERS}
    if not run:
        return {"run": None, "alarms": [], "incidents": [], "edges": store.edge_status(), "oplog": [], "budget": budget}
    rid = run["run_id"]
    return {
        "run": run,
        "alarms": store.alarms_for_run(plant_id, rid, after_sk=after),
        "incidents": store.incidents_for_run(plant_id, rid),
        "edges": store.edge_status(),
        "oplog": store.oplog_for_run(plant_id, rid),
        "running": bool((t := _running.get(plant_id)) and not t.done()),
        "budget": budget,
    }


class ChatRequest(BaseModel):
    plant_id: str
    run_id: str
    question: str
    history: list[dict] = []


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request, x_demo_passcode: str | None = Header(default=None)):
    _guard(x_demo_passcode, request, "chat", per_hour=40)
    if req.plant_id not in PROFILES or not req.question.strip():
        raise HTTPException(400, "bad request")
    t0 = time.time()
    answer, usage = await asyncio.to_thread(rca_agent.chat, req.plant_id, req.run_id, req.question[:500], req.history)
    return {"answer": answer, "usage": usage, "ms": round((time.time() - t0) * 1000)}


@app.get("/api/benchmark")
def benchmark():
    bucket = os.environ.get("ARTIFACTS_BUCKET")
    try:
        if bucket:
            return JSONResponse(json.loads(_s3.get_object(Bucket=bucket, Key="benchmark/latest.json")["Body"].read()))
    except _s3.exceptions.NoSuchKey:
        pass
    local = Path(__file__).resolve().parents[2] / "bench" / "results" / "latest.json"
    return JSONResponse(json.loads(local.read_text()) if local.exists() else {"available": False})
