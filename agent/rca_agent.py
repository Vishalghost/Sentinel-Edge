"""The generative half: a Strands agent on Amazon Bedrock (Nova Pro by default; Claude via BEDROCK_MODEL_ID).

Only alarms the edge escalates ever reach this code -- that is the point of the
Decider gate. The agent writes a root-cause analysis for an incident and answers
operator questions, using read-only tools over DynamoDB and the plant profile.
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from alarm_triage import load_all

from . import store

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
PROFILES = load_all()


class RootCause(BaseModel):
    title: str = Field(description="One-line incident title for the control-room board")
    root_cause: str = Field(description="Most likely initiating event and why, in 2-3 sentences")
    evidence: list[str] = Field(description="Alarm tags / log entries that support the root cause")
    hazard: Literal["none", "process", "safety"] = Field(description="safety if people, product safety or containment are at risk")
    next_steps: list[str] = Field(description="Up to 4 concrete operator actions, most urgent first")
    confidence: Literal["low", "medium", "high"]


def _tools(plant_id: str, run_id: str):
    profile = PROFILES[plant_id]

    @tool
    def get_alarm_history() -> str:
        """All alarms in this run with the edge (Decider) decision: category, route and signals."""
        rows = store.alarms_for_run(plant_id, run_id)
        return "\n".join(
            f"{r['tag']} [{r['priority']}] {r['text']} | {r['equipment']} | Decider: {r['category']} -> {r['route']}"
            for r in rows
        ) or "no alarms yet"

    @tool
    def get_operator_log() -> str:
        """Operator / maintenance log entries for this run."""
        return "\n".join(store.oplog_for_run(plant_id, run_id)) or "no entries"

    @tool
    def get_equipment(tag: str) -> str:
        """Type, area and process connections (feeds / fed by) of one equipment tag, e.g. P-101."""
        e = profile.equipment.get(tag)
        if not e:
            return f"unknown equipment {tag}; known: {', '.join(profile.equipment)}"
        return f"{tag}: {e['type']} in {profile.areas.get(e['area'])}; feeds {e.get('feeds') or 'nothing'}; fed by {profile.upstream_of(tag) or 'nothing'}"

    @tool
    def get_incidents() -> str:
        """Incidents already opened in this run with their root-cause analysis."""
        incs = store.incidents_for_run(plant_id, run_id)
        return "\n".join(f"{i['incident_id']}: {i.get('rca', {}).get('title', 'pending')} -> {i.get('rca', {}).get('root_cause', '')}" for i in incs) or "none"

    return [get_alarm_history, get_operator_log, get_equipment, get_incidents]


def _system_prompt(plant_id: str) -> str:
    p = PROFILES[plant_id]
    return (
        f"You are the control-room assistant for {p.display_name} ({p.industry}). {p.description}\n"
        "An edge decision model (Strands Decider 2B) has already triaged every alarm into safety_critical, "
        "actionable, duplicate_of_active or nuisance and routed it (escalate / review / suppress). "
        "Use the tools to read alarm history, the operator log and equipment connections. "
        "Be concrete and brief; operators are busy. Never invent alarm tags or equipment."
    )


def _model() -> BedrockModel:
    return BedrockModel(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION", "us-east-1"), temperature=0.2)


def _usage(result) -> dict:
    u = getattr(result, "metrics", None)
    acc = getattr(u, "accumulated_usage", {}) if u else {}
    return {"input_tokens": acc.get("inputTokens", 0), "output_tokens": acc.get("outputTokens", 0)}


def root_cause(plant_id: str, run_id: str, trigger_alarms: list[dict]) -> tuple[dict, dict]:
    """Return (RCA dict, token usage) for an incident triggered by the given escalated alarms."""
    agent = Agent(model=_model(), system_prompt=_system_prompt(plant_id), tools=_tools(plant_id, run_id), callback_handler=None)
    trigger = "\n".join(f"- {a['tag']} [{a['priority']}] {a['text']} (Decider: {a['category']}, signals {a.get('signals')})" for a in trigger_alarms)
    result = agent(
        f"These alarms were escalated by the edge and opened an incident:\n{trigger}\n"
        "Check the alarm history and operator log, then give the most likely root cause and next steps.",
        structured_output_model=RootCause,
    )
    return result.structured_output.model_dump(), _usage(result)


def chat(plant_id: str, run_id: str, question: str, history: list[dict] | None = None) -> tuple[str, dict]:
    agent = Agent(
        model=_model(),
        system_prompt=_system_prompt(plant_id) + " Answer in at most 5 short sentences or bullets.",
        tools=_tools(plant_id, run_id),
        messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in (history or [])[-6:]],
        callback_handler=None,
    )
    result = agent(question)
    return str(result).strip(), _usage(result)
