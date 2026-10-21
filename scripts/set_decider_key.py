"""Copy STRANDS_DECIDER_API_KEY from .env into the deployed DeciderApiKey secret (run after cdk deploy).

  python scripts/set_decider_key.py            # verifies the key against the hosted API first
  python scripts/set_decider_key.py --no-check

The key is read from .env or the environment and never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decider_policy import DeciderClient  # noqa: E402  (importing it loads .env)

STACK = "AlarmTriage"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--no-check", action="store_true", help="skip the GET /v1/models key check")
    args = ap.parse_args()

    key = os.environ.get("STRANDS_DECIDER_API_KEY", "").strip()
    if not key:
        sys.exit("STRANDS_DECIDER_API_KEY is empty: paste it into .env (copy .env.example) and rerun.")
    if not args.no_check:
        client = DeciderClient(api_key=key)
        if any(h in client.url for h in ("127.0.0.1", "localhost")):
            sys.exit("STRANDS_DECIDER_URL points at a local server; set it to the hosted API URL in .env.")
        if not client.health():
            sys.exit(f"The hosted API rejected the key or is down ({client.url}/v1/models). Secret not updated.")

    outputs = boto3.client("cloudformation", region_name=args.region).describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    arn = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "DeciderApiKeySecret")
    boto3.client("secretsmanager", region_name=args.region).put_secret_value(SecretId=arn, SecretString=key)
    print(f"Updated {arn.rsplit(':', 1)[-1]} ({len(key)} chars). Next: cdk deploy -c cloud_edge=1")


if __name__ == "__main__":
    main()
