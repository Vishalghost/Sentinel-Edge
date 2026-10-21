# Cloud edge: the same edge agent, deciding through the hosted Decider API (no GPU, no torch).
# Build context: repo root (see infra/stack.py)
FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir awsiotsdk pyyaml
COPY decider_policy/ decider_policy/
COPY alarm_triage/ alarm_triage/
COPY profiles/ profiles/
COPY edge/ edge/
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "edge.edge_agent"]
