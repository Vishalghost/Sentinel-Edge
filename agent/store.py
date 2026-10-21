"""DynamoDB access shared by the incident Lambda and the control-plane app.

alarms table    pk plant_id   sk "{run_id}#{ts_ms}#{alarm_id}"   (written by the IoT Rule)
                pk "__edge__" sk edge_id                         (edge heartbeat)
incidents table pk plant_id   sk "INC#{run_id}#{incident_id}"    (incident + RCA)
                              sk "OPLOG#{run_id}#{ts_ms}"        (operator log notes)
                              sk "RUN#{run_id}"                  (run metadata)
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _alarms():
    return _ddb.Table(os.environ["ALARMS_TABLE"])


def _incidents():
    return _ddb.Table(os.environ["INCIDENTS_TABLE"])


def plain(obj: Any) -> Any:
    """DynamoDB Decimals -> int/float, recursively."""
    if isinstance(obj, list):
        return [plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def ddb_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj), parse_float=Decimal)


def _query_all(table, **kwargs) -> list[dict]:
    items, resp = [], table.query(**kwargs)
    items += resp["Items"]
    while "LastEvaluatedKey" in resp:
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items += resp["Items"]
    return plain(items)


def alarms_for_run(plant_id: str, run_id: str, after_sk: str | None = None) -> list[dict]:
    """Alarms of one run in time order; with after_sk, only those newer than that sort key."""
    if after_sk:
        cond = Key("plant_id").eq(plant_id) & Key("sk").between(after_sk, run_id + "#~")
    else:
        cond = Key("plant_id").eq(plant_id) & Key("sk").begins_with(run_id + "#")
    items = _query_all(_alarms(), KeyConditionExpression=cond)
    return [i for i in items if not after_sk or i["sk"] > after_sk]


def edge_status() -> list[dict]:
    return _query_all(_alarms(), KeyConditionExpression=Key("plant_id").eq("__edge__"))


def put_incident(item: dict) -> None:
    _incidents().put_item(Item=ddb_safe(item))


def incidents_for_run(plant_id: str, run_id: str) -> list[dict]:
    return _query_all(_incidents(), KeyConditionExpression=Key("plant_id").eq(plant_id) & Key("sk").begins_with(f"INC#{run_id}#"))


def put_oplog(plant_id: str, run_id: str, note: str) -> None:
    _incidents().put_item(Item={"plant_id": plant_id, "sk": f"OPLOG#{run_id}#{int(time.time() * 1000):013d}", "note": note})


def oplog_for_run(plant_id: str, run_id: str) -> list[str]:
    items = _query_all(_incidents(), KeyConditionExpression=Key("plant_id").eq(plant_id) & Key("sk").begins_with(f"OPLOG#{run_id}#"))
    return [i["note"] for i in items]


def put_run(plant_id: str, run_id: str, meta: dict) -> None:
    _incidents().put_item(Item=ddb_safe({"plant_id": plant_id, "sk": f"RUN#{run_id}", **meta}))


def latest_run(plant_id: str) -> dict | None:
    items = _query_all(_incidents(), KeyConditionExpression=Key("plant_id").eq(plant_id) & Key("sk").begins_with("RUN#"))
    return max(items, key=lambda i: i.get("started", 0)) if items else None
