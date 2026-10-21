#!/usr/bin/env bash
# One-time: add edge runtime deps to the WSL venv.
export PATH="$HOME/.local/bin:$PATH"
cd ~/strands && . .venv/bin/activate
uv pip install awsiotsdk pytest pyyaml 2>&1 | tail -3
python -c "import awsiot, awscrt; print('awsiot ok')"
