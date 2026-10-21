# Sentinel Edge — alarm-flood triage that decides at the edge and generates in the core

**A factory alarm agent that uses Strands Decider 2B to decide, in under a second on a laptop GPU, which alarms an operator actually needs to see, and calls Amazon Bedrock only for the few that need an explanation.**

Decision pattern: **`#policy`** · Tags: `#strands-decider-2b` `#policy`

Control rooms drown in alarms. One tripped pump becomes forty alarms in a minute (ISA-18.2 calls this an *alarm flood*), and the one that matters, such as a relief valve lifting, scrolls past. This agent puts a small decision model on the plant's edge box to triage every alarm as it arrives, and sends only the real incidents to an LLM for root-cause analysis.

![architecture](docs/architecture.png)

## What Decider 2B decides

For **every alarm**, Decider answers four yes/no signals in about 0.9 s on a laptop RTX 4050:

| signal | question (abridged) | context it sees |
|---|---|---|
| `danger` | Does this alarm itself signal an immediate hazard? | the alarm only |
| `noise` | Is it chattering / repeating without a process change? | the alarm only |
| `maintenance` | Is its equipment under maintenance per the operator log? | alarm + operator log |
| `consequence` | Is it a downstream consequence of an already-active alarm? | alarm + active alarms + equipment topology + log |

Plain Python ([`alarm_triage/router.py`](alarm_triage/router.py)) turns the signals into a category (`safety_critical`, `actionable`, `duplicate_of_active`, `nuisance`) and a route:

- **escalate** → SQS → Lambda → a Strands agent on **Amazon Bedrock** writes the root cause and next steps
- **review** → shown to the operator, no LLM
- **suppress** → recorded, greyed out, no LLM

Uncertain danger is never suppressed, and if Decider is down, every alarm goes to review.

## Results (49 labelled alarms, 3 industries)

From [`bench/results/RESULTS.md`](bench/results/RESULTS.md) (LLM = Amazon Nova Pro on Bedrock; Decider on a laptop RTX 4050 in WSL2):

| | Decider-gated agent | LLM-only agent |
|---|---|---|
| category accuracy | 81.6 % | 81.6 % (87.8 % on an earlier run: the LLM varies between runs) |
| real alarms hidden from the operator | **0** | **0** |
| safety alarms surfaced to the operator | 5/5 (4 labelled safety-critical, 1 sent to review) | 5/5 |
| decision latency p50 / p95 | **955 / 1146 ms** | 1695 / 2638 ms |
| LLM calls | **11** | 49 |
| LLM tokens (in + out) | **4.1 k** | 50.1 k |
| LLM cost per 1,000 alarms (assumed $0.80 / $3.20 per M tokens) | **$0.11** | $1.17 |

The two arms make *different* mistakes: the LLM over-escalates (duplicates → actionable), while Decider's misses go to *review*. What we learned getting there:

| design (same model, same alarms) | accuracy |
|---|---|
| one 4-way `choice` question | 45 % |
| four yes/no signals, full context | 90 % on the tuning plant, **57-60 % on held-out plants** |
| four yes/no signals, **scoped context per signal** | 85 % tuning, **79-80 % held out** |

Small models answer the question the context suggests. Scoping the state per signal was the single biggest improvement. See [`decider_policy/README.md`](decider_policy/README.md).

## ⭐ Reusable Strands Decider 2B decision pattern: PolicyGate (`#policy`)

**→ [`decider_policy/README.md`](decider_policy/README.md)**: the typed questions and option sets, the wiring into the agent's path (a Strands `InterventionHandler`), and how to use it in your own agent.

[`decider_policy/`](decider_policy/) is industry-agnostic: `PolicyGate` (typed yes/no signals → your `classify` → your `route`) plus `PolicyGateIntervention`, a Strands `InterventionHandler` that returns `Deny` in `before_invocation`, so the LLM is never called for suppressed requests.

## Plug-and-play industries

All plant knowledge lives in YAML ([`profiles/`](profiles/)): equipment and topology, alarm catalog, the signal wording, and labelled scenarios. Three ship: a chemical process plant, a food & beverage bottling line, and an automotive weld cell. Adding an industry means adding a file.

## Repository map

| path | what |
|---|---|
| `decider_policy/` | the reusable Decider pattern (client, gate, Strands intervention) |
| `alarm_triage/` | profiles, scoped state builder, signal→category→route policy, engine |
| `edge/` | `edge_agent.py` (MQTT to IoT Core) and `local_run.py` (no AWS needed) |
| `agent/` | Strands agent on Bedrock: RCA + chat with DynamoDB tools |
| `cloud/app/` | FastAPI dashboard + simulator + chat (Fargate) |
| `cloud/incident/` | SQS-triggered incident Lambda |
| `infra/` | AWS CDK stack (IoT rules, DynamoDB, SQS, Lambda, ALB/Fargate, IAM) |
| `bench/` | Decider-gated vs LLM-only benchmark |
| `tests/` | 25 unit/smoke tests (no GPU, no AWS) |

## Run it

### 1. Edge (WSL2 Ubuntu with an NVIDIA GPU)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH=$HOME/.local/bin:$PATH
mkdir -p ~/strands && cd ~/strands && uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install strands-decider strands-agents boto3 awsiotsdk pyyaml pytest
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099     # leave running
# second terminal, from the repo:
python -m edge.local_run process_plant          # triage scenarios locally, no AWS
```

### 2. Core (Windows/macOS/Linux with AWS credentials, Docker running, Node 18+)
```bash
python -m venv .venv-win && .venv-win/Scripts/pip install -r infra/requirements.txt
cd infra && npx aws-cdk@latest deploy          # prints DashboardUrl
# optional: -c model_id=us.anthropic.claude-sonnet-4-5-20250929-v1:0   -c passcode=...
```

### 3. Connect the edge
```bash
python scripts/provision_edge.py --out \\wsl.localhost\Ubuntu\home\<you>\strands\certs   # X.509 cert + policy + thing
# in WSL, from the repo:
python -m edge.edge_agent --endpoint $(cat ~/strands/certs/endpoint.txt)
```
Open the `DashboardUrl`, pick a plant, and press **Start alarm flood**.

### 4. Optional: cloud edge (live demo while the laptop is off)

The same edge agent runs as a small Fargate container that asks a **hosted** Decider API (here the AWS User Group Madurai endpoint: `POST /v1/decide`, `Authorization: Bearer <key>`). The core assigns every run to exactly one online edge, preferring the free laptop GPU, so an alarm is never decided twice.

```bash
# after the first deploy (outputs DeciderApiKeySecret and EdgeCloudCertSecret):
aws secretsmanager put-secret-value --secret-id <DeciderApiKeySecret> --secret-string "<your key>"
python scripts/provision_edge.py --thing edge-cloud --secret <EdgeCloudCertSecret>
cd infra && npx aws-cdk@latest deploy -c cloud_edge=1      # optional: -c decider_rpm=9 -c decider_max_chars=4000
```

The free tier is metered (2,000 answers a month, 1 per question, so 4 per alarm), so the core reserves a run's answers in DynamoDB before it starts (`DECIDER_MONTHLY_ANSWERS`, default 1,800) and caps cloud runs per day (`CLOUD_DAILY_RUN_CAP`, default 8). When the budget is spent, the dashboard keeps showing the most recent recorded run. The client paces requests (`STRANDS_DECIDER_RPM`), allows one request at a time, retries 429s, and checks health with `GET /v1/models`, which spends no answers.

### Benchmark and tests
```bash
python bench/benchmark.py --price-in 0.8 --price-out 3.2   # needs Decider + Bedrock
python -m pytest -q
```

### Tear down
`cd infra && npx aws-cdk@latest destroy` (the IoT certificate made by `provision_edge.py` must be detached and deleted separately).

## Security notes

- The edge connects **outbound only** (MQTT/TLS 8883) with an X.509 certificate. Its IoT policy can only publish decisions and its own heartbeat.
- The public dashboard is rate-limited per IP and globally per day, and an optional passcode protects runs and chat. The ALB serves HTTP; add an ACM certificate and a domain for HTTPS.
- Scenario labels travel with alarms for scoring but are never put in Decider's state.

## Limits

49 labelled alarms is a small evaluation, and the thresholds were tuned on one plant. Decider 2B is an experimental model, so treat this as a working prototype, not a safety system: alarm suppression in a real plant needs a formal alarm-management review (ISA-18.2 / IEC 62682).
Copyright 2026 the Sentinel Edge authors. Licensed under the Apache License, Version 2.0 (see LICENSE).
