# Build context: repo root (see infra/stack.py)
FROM public.ecr.aws/lambda/python:3.12
COPY cloud/incident/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
COPY decider_policy/ ${LAMBDA_TASK_ROOT}/decider_policy/
COPY alarm_triage/ ${LAMBDA_TASK_ROOT}/alarm_triage/
COPY agent/ ${LAMBDA_TASK_ROOT}/agent/
COPY profiles/ ${LAMBDA_TASK_ROOT}/profiles/
COPY cloud/incident/handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]
