"""Minimal client for a Strands Decider endpoint: a local `strands-decider serve` or a hosted API.

Standard library only, so it drops into any agent without new dependencies.
Wire format: POST /v1/systemone {"state": ..., "questions": {name: {...}}}.

Hosted endpoints are metered, so the client can authenticate, pace itself under a
requests-per-minute limit, allow one request at a time, and count answers used.
Two wire formats, picked automatically:
  local `strands-decider serve`  POST /v1/systemone, no key, health = a tiny question
  hosted API (key set)           POST /v1/decide, Authorization: Bearer <key>, health = GET /v1/models
                                 (e.g. the AWS User Group Madurai endpoint: 2,000 answers/month)

Configure by environment (or constructor arguments):
  STRANDS_DECIDER_URL        base URL (default http://127.0.0.1:8099; DECIDER_URL also accepted)
  STRANDS_DECIDER_API_KEY    API key; unset for a local server (DECIDER_API_KEY also accepted)
  STRANDS_DECIDER_AUTH       "bearer" (Authorization: Bearer <key>) or a header name, e.g. "x-api-key"
  STRANDS_DECIDER_RPM        requests per minute to stay under (0 = unlimited)
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_URL = os.environ.get("STRANDS_DECIDER_URL") or os.environ.get("DECIDER_URL") or "http://127.0.0.1:8099"


class DeciderUnavailable(RuntimeError):
    """No Decider answered (down, unreachable, rate-limited after retries, or quota exhausted)."""


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
    api_key: str | None = field(default_factory=lambda: os.environ.get("STRANDS_DECIDER_API_KEY") or os.environ.get("DECIDER_API_KEY") or None)
    auth: str = field(default_factory=lambda: os.environ.get("STRANDS_DECIDER_AUTH", "bearer"))
    rpm: float = field(default_factory=lambda: float(os.environ.get("STRANDS_DECIDER_RPM", "0")))
    timeout_s: float = 30.0
    retries: int = 3
    calls: int = field(default=0, init=False)
    answers: int = field(default=0, init=False)  # hosted APIs bill per question answered

    def __post_init__(self):
        self._lock = threading.Lock()  # one request at a time
        self._next_slot = 0.0

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            if self.auth.lower() == "bearer":
                h["Authorization"] = f"Bearer {self.api_key}"
            else:
                h[self.auth] = self.api_key
        return h

    def _pace(self) -> None:
        if self.rpm > 0:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_slot = time.monotonic() + 60.0 / self.rpm

    def ask(self, state: Any, questions: dict[str, dict]) -> dict:
        """One state, many questions: the state is encoded once per request."""
        body = json.dumps({"state": state, "questions": questions}).encode()
        with self._lock:
            for attempt in range(self.retries + 1):
                self._pace()
                path = "/v1/decide" if self.metered else "/v1/systemone"
                req = urllib.request.Request(self.url.rstrip("/") + path, data=body, headers=self._headers())
                t0 = time.perf_counter()
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                        resp = json.loads(r.read())
                    break
                except urllib.error.HTTPError as e:
                    detail = e.read()[:300]
                    if e.code == 429 and attempt < self.retries:  # rate limited: back off and retry
                        retry_after = e.headers.get("Retry-After") or ""
                        time.sleep(float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1))
                        continue
                    if e.code >= 500 and attempt < self.retries:
                        time.sleep(2 ** attempt)
                        continue
                    if e.code in (401, 402, 403, 429) or e.code >= 500:  # bad key, quota, still limited, still busy
                        raise DeciderUnavailable(f"Decider refused the request: {e.code} {detail!r}") from e
                    raise ValueError(f"Decider rejected the request: {e.code} {detail!r}") from e
                except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                    raise DeciderUnavailable(f"Decider not reachable at {self.url}: {e}") from e
        self.calls += 1
        self.answers += len(questions)
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
        """Liveness check. Hosted: GET /v1/models (spends no answers). Local: one tiny question."""
        if self.metered:
            req = urllib.request.Request(self.url.rstrip("/") + "/v1/models", headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=15) as r:  # unpaced: rpm default leaves 1/min for this
                    return r.status == 200
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                return False
        try:
            self.noul("health check", "Is this a health check?")
            return True
        except DeciderUnavailable:
            return False

    @property
    def metered(self) -> bool:
        return bool(self.api_key)
