"""Edge triage agent: the laptop plays a factory edge box.

Outbound-only MQTT/TLS to AWS IoT Core with an X.509 certificate -- no inbound ports.
  subscribes  plants/+/alarms   plants/+/oplog   plants/+/control
  publishes   plants/{plant}/decisions           edge/{edge_id}/status (heartbeat)

The same code runs as two kinds of edge (the core assigns each run to exactly one):
  laptop GPU   python -m edge.edge_agent --endpoint xxxx-ats.iot.us-east-1.amazonaws.com
  cloud        container on Fargate, Decider = hosted API (STRANDS_DECIDER_API_KEY set),
               certificate from EDGE_CERT_PEM / EDGE_KEY_PEM
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import tempfile
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from awscrt import mqtt  # noqa: E402
from awsiot import mqtt_connection_builder  # noqa: E402

from alarm_triage import PlantTriage, decision_message, load_all  # noqa: E402
from decider_policy import DeciderClient  # noqa: E402

log = logging.getLogger("edge")
HEARTBEAT_S = 5
METERED_HEALTH_EVERY_S = 120  # even a free health GET counts against a hosted rate limit


class EdgeAgent:
    def __init__(self, conn, edge_id: str, client: DeciderClient):
        self.conn = conn
        self.edge_id = edge_id
        self.client = client
        self.profiles = load_all()
        self.plants = {pid: PlantTriage(p, client) for pid, p in self.profiles.items()}
        self.inbox: queue.Queue = queue.Queue()
        self.latencies: deque = deque(maxlen=100)
        self.decisions = 0
        self.kind = "api" if client.metered else "gpu"
        self.decider_ok = True
        self._last_health = 0.0
        self.stop = threading.Event()

    # MQTT callbacks run on the SDK's thread: only enqueue there, decide on our worker.
    def on_message(self, topic, payload, **_):
        try:
            self.inbox.put((topic, json.loads(payload)))
        except json.JSONDecodeError:
            log.warning("dropping non-JSON message on %s", topic)

    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                topic, msg = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle(topic, msg)
            except Exception:  # one bad message must not kill the edge
                log.exception("failed to handle %s", topic)

    def handle(self, topic: str, msg: dict) -> None:
        _, plant_id, kind = topic.split("/", 2)
        triage = self.plants.get(plant_id)
        if triage is None:
            log.warning("unknown plant %s (no profile on this edge)", plant_id)
            return
        if kind == "control" and msg.get("action") == "reset":
            triage.state.reset()
            log.info("[%s] reset for run %s", plant_id, msg.get("run_id"))
        elif kind == "oplog":
            triage.state.add_oplog(msg["note"])
            log.info("[%s] oplog: %s", plant_id, msg["note"])
        elif kind == "alarms":
            target = msg.get("edge_target")
            if target and target != self.edge_id:
                return  # the core assigned this run to another edge: never decide twice
            d, _ = triage.decide(msg)
            self.decider_ok = not d.degraded
            out = decision_message(msg, d, self.edge_id)
            self.conn.publish(topic=f"plants/{plant_id}/decisions", payload=json.dumps(out), qos=mqtt.QoS.AT_LEAST_ONCE)
            self.decisions += 1
            if not d.degraded:
                self.latencies.append(d.latency_ms)
            log.info("[%s] %-14s %-20s -> %-8s %4.0f ms", plant_id, msg["tag"], d.category, d.route, d.latency_ms)

    def heartbeat(self) -> None:
        while not self.stop.wait(HEARTBEAT_S):
            lat = list(self.latencies)
            if not self.client.metered or time.time() - self._last_health > METERED_HEALTH_EVERY_S:
                self.decider_ok = self.client.health()
                self._last_health = time.time()
            status = {
                "plant_id": "__edge__",
                "sk": self.edge_id,
                "edge_id": self.edge_id,
                "online": True,
                "decider_ok": self.decider_ok,
                "kind": self.kind,
                "gpu": os.environ.get("EDGE_GPU", "hosted Decider API" if self.kind == "api" else "local GPU"),
                "decisions_total": self.decisions,
                "answers_used": self.client.answers,
                "p50_ms": round(statistics.median(lat), 1) if lat else None,
                "ts": time.time(),
            }
            self.conn.publish(topic=f"edge/{self.edge_id}/status", payload=json.dumps(status), qos=mqtt.QoS.AT_MOST_ONCE)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("IOT_ENDPOINT"), required=os.environ.get("IOT_ENDPOINT") is None)
    ap.add_argument("--cert-dir", default=os.path.expanduser("~/strands/certs"))
    ap.add_argument("--edge-id", default=os.environ.get("EDGE_ID", "edge-laptop"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    client = DeciderClient()
    if not client.health():
        sys.exit(f"Decider is not answering at {client.url}. Start it first: bash .serve.sh")

    certs = Path(args.cert_dir)
    if os.environ.get("EDGE_CERT_PEM"):  # container: certificate injected from Secrets Manager
        certs = Path(tempfile.mkdtemp(prefix="edge-cert-"))
        (certs / "edge.cert.pem").write_text(os.environ["EDGE_CERT_PEM"])
        (certs / "edge.private.key").write_text(os.environ["EDGE_KEY_PEM"])
        (certs / "edge.private.key").chmod(0o600)
    ca = certs / "AmazonRootCA1.pem"
    conn = mqtt_connection_builder.mtls_from_path(
        endpoint=args.endpoint,
        cert_filepath=str(certs / "edge.cert.pem"),
        pri_key_filepath=str(certs / "edge.private.key"),
        ca_filepath=str(ca) if ca.exists() else None,  # else the system trust store (has Amazon Root CA 1)
        client_id=args.edge_id,
        clean_session=True,
        keep_alive_secs=30,
        on_connection_interrupted=lambda *a, **k: log.warning("connection interrupted, SDK will retry"),
        on_connection_resumed=lambda *a, **k: log.info("connection resumed"),
    )
    agent = EdgeAgent(conn, args.edge_id, client)
    conn.connect().result()
    log.info("connected to %s as %s; plants: %s", args.endpoint, args.edge_id, ", ".join(agent.plants))
    for t in ("plants/+/alarms", "plants/+/oplog", "plants/+/control"):
        fut, _ = conn.subscribe(topic=t, qos=mqtt.QoS.AT_LEAST_ONCE, callback=agent.on_message)
        fut.result()
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.heartbeat, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop.set()
        offline = {"plant_id": "__edge__", "sk": args.edge_id, "edge_id": args.edge_id, "online": False, "ts": time.time()}
        conn.publish(topic=f"edge/{args.edge_id}/status", payload=json.dumps(offline), qos=mqtt.QoS.AT_LEAST_ONCE).result()
        conn.disconnect().result()


if __name__ == "__main__":
    main()
