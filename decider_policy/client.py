"""Minimal client for a Strands Decider server (`strands-decider serve`).

Standard library only, so it drops into any agent without new dependencies.
Wire format: POST /v1/systemone {"state": ..., "questions": {name: {...}}}.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_URL = os.environ.get("STRANDS_DECIDER_URL", "http://127.0.0.1:8099")


class DeciderUnavailable(RuntimeError):
    """No Decider server answered. Start one with `strands-decider serve <checkpoint>`."""


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float  # normalised max-probability: 0 = uniform, 1 = certain
    probabilities: dict[str, float]
    latency_ms: float
    model: str = ""
    input_tokens: int = 0


@dataclass
class DeciderClient:
    url: str = DEFAULT_URL
    timeout_s: float = 30.0
    calls: int = field(default=0, init=False)

    def ask(self, state: Any, questions: dict[str, dict]) -> dict:
        """One state, many questions: the state is encoded once per request."""
        body = json.dumps({"state": state, "questions": questions}).encode()
        req = urllib.request.Request(
            self.url.rstrip("/") + "/v1/systemone",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ValueError(f"Decider rejected the request: {e.code} {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise DeciderUnavailable(f"Decider not reachable at {self.url}: {e}") from e
        self.calls += 1
        resp["_wall_ms"] = (time.perf_counter() - t0) * 1000
        return resp

    def choice(self, state: Any, instructions: str, options: dict[str, str]) -> ChoiceResult:
        resp = self.ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}})
        a = resp["answers"]["q"]
        return ChoiceResult(
            choice=a["choice"],
            confidence=float(a["confidence"]),
            probabilities={k: float(v) for k, v in a["probabilities"].items()},
            latency_ms=float(resp.get("latency_ms") or resp["_wall_ms"]),
            model=resp.get("model", ""),
            input_tokens=int(resp.get("usage", {}).get("input_tokens", 0)),
        )

    def noul(self, state: Any, instructions: str, criteria: dict[str, str] | None = None) -> float:
        q: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            q["criteria"] = criteria
        return float(self.ask(state, {"q": q})["answers"]["q"]["noul"])

    def health(self) -> bool:
        try:
            self.noul("health check", "Is this a health check?")
            return True
        except DeciderUnavailable:
            return False
