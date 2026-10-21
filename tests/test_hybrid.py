"""Hybrid edges (laptop GPU + hosted Decider API): routing, isolation and quota safety. No network."""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALARMS_TABLE", "test-alarms")
os.environ.setdefault("INCIDENTS_TABLE", "test-incidents")

from alarm_triage import PlantState, load_all, make_alarm  # noqa: E402
from cloud.app.main import pick_edge  # noqa: E402
from decider_policy import DeciderClient  # noqa: E402

GPU = {"edge_id": "edge-laptop", "kind": "gpu", "decider_ok": True}
API = {"edge_id": "edge-cloud", "kind": "api", "decider_ok": True}


def test_auto_prefers_the_free_gpu_edge():
    assert pick_edge([API, GPU])["edge_id"] == "edge-laptop"


def test_auto_falls_back_to_the_api_when_laptop_is_off():
    assert pick_edge([API])["edge_id"] == "edge-cloud"


def test_auto_skips_a_gpu_edge_whose_decider_is_down():
    assert pick_edge([dict(GPU, decider_ok=False), API])["edge_id"] == "edge-cloud"


def test_explicit_choice_and_no_edge():
    assert pick_edge([API, GPU], "edge-cloud")["edge_id"] == "edge-cloud"
    assert pick_edge([], "auto") is None


class _Conn:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos):
        self.published.append((topic, json.loads(payload)))


class _Client:
    metered = False
    answers = 0

    def ask(self, state, questions):
        return {"answers": {k: {"type": "noul", "noul": 0.1} for k in questions}, "latency_ms": 5.0}

    def health(self):
        return True


def test_an_edge_ignores_alarms_assigned_to_another_edge():
    from edge.edge_agent import EdgeAgent  # needs awsiotsdk; installed in both venvs

    conn = _Conn()
    agent = EdgeAgent(conn, "edge-laptop", _Client())
    p = load_all()["process_plant"]
    mine = dict(make_alarm(p, "LSH-305", "r", 0), edge_target="edge-laptop")
    theirs = dict(make_alarm(p, "LSH-305", "r", 1), edge_target="edge-cloud")
    agent.handle("plants/process_plant/alarms", theirs)
    assert conn.published == []  # never decide twice
    agent.handle("plants/process_plant/alarms", mine)
    assert len(conn.published) == 1 and conn.published[0][1]["edge_id"] == "edge-laptop"


def test_hosted_client_uses_decide_path_and_bearer(monkeypatch):
    seen = {}

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}).encode()

    def fake_urlopen(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c = DeciderClient(url="https://hosted.example", api_key="k123", rpm=0)
    c.noul("s", "q?")
    assert seen["url"].endswith("/v1/decide") and seen["auth"] == "Bearer k123"
    assert c.answers == 1
    assert c.health() and seen["url"].endswith("/v1/models")  # health spends no answers
    assert c.answers == 1


def test_local_client_keeps_systemone_and_no_auth(monkeypatch):
    c = DeciderClient(url="http://127.0.0.1:8099", api_key=None)
    assert not c.metered and "Authorization" not in c._headers()


def test_state_respects_a_character_budget():
    p = load_all()["process_plant"]
    st = PlantState(p)
    for i, tag in enumerate(["FI-301-LO", "TI-302-HI", "TI-204-HI", "LI-203-HI", "PI-205-HI", "LSH-305"]):
        st.record(make_alarm(p, tag, "r", i, ts=1000.0 + i), "duplicate_of_active")
    new = make_alarm(p, "PSV-205-OPEN", "r", 9, ts=1010.0)
    full = st.build(new)["full"]
    small = st.build(new, {"full": 900})["full"]
    assert len(full) > 900 >= len(small)
    assert "PSV-205-OPEN" in small  # the new alarm is never trimmed away
