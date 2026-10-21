"""Smoke tests for the control-plane app: imports, static routes, abuse guards. No AWS calls."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ALARMS_TABLE", "test-alarms")
os.environ.setdefault("INCIDENTS_TABLE", "test-incidents")
os.environ.setdefault("AWS_REGION", "us-east-1")

from fastapi.testclient import TestClient  # noqa: E402

from cloud.app import main  # noqa: E402

client = TestClient(main.app)


def test_index_and_health():
    assert client.get("/health").json() == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200 and "Alarm Flood Triage" in r.text


def test_profiles_lists_all_three_industries():
    body = client.get("/api/profiles").json()
    assert {p["profile_id"] for p in body["profiles"]} == {"process_plant", "bottling_line", "discrete_assembly"}


def test_unknown_plant_is_rejected_before_any_aws_call():
    assert client.post("/api/runs", json={"plant_id": "nope", "scenario_id": "x"}).status_code == 404


def test_rate_limit_kicks_in(monkeypatch):
    main._hits.clear()
    req = type("R", (), {"client": type("C", (), {"host": "1.2.3.4"})()})()
    for _ in range(3):
        main._guard(None, req, "chat", per_hour=3)
    with pytest.raises(main.HTTPException) as e:
        main._guard(None, req, "chat", per_hour=3)
    assert e.value.status_code == 429


def test_passcode_enforced(monkeypatch):
    monkeypatch.setattr(main, "PASSCODE", "s3cret")
    with pytest.raises(main.HTTPException) as e:
        main._guard("wrong", None, "chat", per_hour=10)
    assert e.value.status_code == 401
