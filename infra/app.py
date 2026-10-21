#!/usr/bin/env python3
import os

import aws_cdk as cdk
import boto3

from stack import AlarmTriageStack

app = cdk.App()
region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")
endpoint = app.node.try_get_context("iot_endpoint") or boto3.client("iot", region_name=region).describe_endpoint(
    endpointType="iot:Data-ATS")["endpointAddress"]
AlarmTriageStack(
    app, "AlarmTriage",
    iot_endpoint=endpoint,
    # Claude once Marketplace billing works: cdk deploy -c model_id=us.anthropic.claude-sonnet-4-5-20250929-v1:0
    model_id=app.node.try_get_context("model_id") or "us.amazon.nova-pro-v1:0",
    passcode=app.node.try_get_context("passcode") or "",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=region),
)
app.synth()
