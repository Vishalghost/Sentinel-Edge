# Build context: repo root (see infra/stack.py)
FROM python:3.12-slim
WORKDIR /srv
COPY cloud/app/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
COPY decider_policy/ decider_policy/
COPY alarm_triage/ alarm_triage/
COPY agent/ agent/
COPY profiles/ profiles/
COPY cloud/__init__.py cloud/__init__.py
COPY cloud/app/ cloud/app/
EXPOSE 8080
CMD ["uvicorn", "cloud.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
