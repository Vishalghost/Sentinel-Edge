"""Create an X.509 certificate for the edge, attach the IoT policy + thing, write the files.

Run on the machine that has AWS credentials (after `cdk deploy`):
    python scripts/provision_edge.py --out \\\\wsl.localhost\\Ubuntu\\home\\<you>\\strands\\certs

The private key exists only in the output folder; AWS never stores it. Re-running
creates a new certificate (the old one stays active until you deactivate it).
"""
from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import boto3

ROOT_CA = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="folder for edge.cert.pem, edge.private.key, AmazonRootCA1.pem")
    ap.add_argument("--secret", help="instead of files: store {cert, key} in this Secrets Manager secret (cloud edge)")
    ap.add_argument("--policy", default="alarm-triage-edge")
    ap.add_argument("--thing", default="edge-laptop")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    if not (args.out or args.secret):
        ap.error("give --out (laptop edge) or --secret (cloud edge)")
    iot = boto3.client("iot", region_name=args.region)
    cert = iot.create_keys_and_certificate(setAsActive=True)
    iot.attach_policy(policyName=args.policy, target=cert["certificateArn"])
    iot.attach_thing_principal(thingName=args.thing, principal=cert["certificateArn"])
    if args.secret:
        import json
        boto3.client("secretsmanager", region_name=args.region).put_secret_value(
            SecretId=args.secret, SecretString=json.dumps({"cert": cert["certificatePem"], "key": cert["keyPair"]["PrivateKey"]}))
        print(f"certificate {cert['certificateId'][:12]}... attached to {args.thing}; stored in the secret (never on disk)")
        return
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    (out / "edge.cert.pem").write_text(cert["certificatePem"])
    key = out / "edge.private.key"
    key.write_text(cert["keyPair"]["PrivateKey"])
    try:
        key.chmod(0o600)
    except OSError:
        pass
    urllib.request.urlretrieve(ROOT_CA, out / "AmazonRootCA1.pem")
    endpoint = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
    (out / "endpoint.txt").write_text(endpoint)
    print(f"certificate {cert['certificateId'][:12]}... attached to {args.thing} with policy {args.policy}")
    print(f"files written to {out}\nIoT endpoint: {endpoint}")


if __name__ == "__main__":
    main()
