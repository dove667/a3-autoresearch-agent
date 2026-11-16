"""Rebuild decision-critical observations independently of generated summaries.

This run-local receipt index records tool observations, never scientific decisions.
The shared plan and memory remain the authorities for research conclusions.
"""

from __future__ import annotations

import hashlib
import json
import re

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as cm

INDEX = "research-receipts.json"
MAX_RECEIPTS = 96
OBSERVATION_TOOLS = frozenset(
    {
        "job_submit",
        "job_prepare",
        "job_status",
        "job_cancel",
        "job_wait",
        "evaluation_compare",
        "conditions_compare",
        "vision_inspect",
        "export_create",
        "export_status",
        "session_stop",
        "session_cancel",
    }
)


def load():
    path = state_paths.path(INDEX)
    if not path.exists():
        return {"version": 1, "receipts": [], "evicted": 0}
    try:
        state = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise cm.ContextError(
            "Research receipt index is unreadable; original journal retained"
        ) from error
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("receipts"), list)
        or len(state["receipts"]) > MAX_RECEIPTS
        or type(state.get("evicted")) is not int
        or state["evicted"] < 0
    ):
        raise cm.ContextError("Invalid research receipt index; original tool journal retained")
    for receipt in state["receipts"]:
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("call_id"), str)
            or receipt.get("tool") not in OBSERVATION_TOOLS
            or not isinstance(receipt.get("facts"), dict)
            or type(receipt.get("omitted_facts")) is not int
            or type(receipt.get("facts_complete")) is not bool
            or not isinstance(receipt.get("source"), str)
            or not re.fullmatch(
                r"context-history/tool-output-[0-9a-f]{64}\.json", receipt["source"]
            )
            or receipt.get("sha256") != receipt["source"].rsplit("-", 1)[-1].removesuffix(".json")
        ):
            raise cm.ContextError("Invalid research receipt; inspect preserved tool journal")
    return state


def record(call, result):
    """Index an executed observation once, with its immutable complete source."""
    if call.get("name") not in OBSERVATION_TOOLS or not isinstance(result, dict):
        return
    original = cm._original_tool_output(cm.output_message(result, call["call_id"]))
    # The same bounded semantic view handles ordinary results and decoded JSON artifacts.
    semantic = json.loads(cm.archived_output(original, 12000)["output"])
    fields = semantic["facts"]
    omitted, complete = semantic["omitted_facts"], semantic["facts_complete"]
    if not fields:
        return
    source = semantic["path"]
    receipt = {
        "call_id": call["call_id"],
        "tool": call["name"],
        "source": source,
        "sha256": source.rsplit("-", 1)[-1].removesuffix(".json"),
        "facts": fields,
        "omitted_facts": omitted,
        "facts_complete": complete and omitted == 0,
    }
    state = load()
    previous = next(
        (item for item in state["receipts"] if item["call_id"] == call["call_id"]), None
    )
    if previous:
        # Recovery may only have the bounded view; the original indexed receipt wins.
        if previous["source"] != source:
            raise cm.ContextError("Research receipt identity changed; inspect original journal")
        return
    state["receipts"].append(receipt)
    if len(state["receipts"]) > MAX_RECEIPTS:
        state["receipts"].pop(0)
        state["evicted"] += 1
    cm.atomic(state_paths.path(INDEX), json.dumps(state, ensure_ascii=False))


def _bounded_items(items, count, budget):
    selected = []
    for item in items:
        if len(selected) >= count:
            break
        if cm.size(selected + [item]) <= budget:
            selected.append(item)
    return selected


def _decision_view(item):
    value = {key: item[key] for key in ("id", "route", "decision", "status") if key in item}
    text = item.get("conclusion", "")
    value["conclusion_preview"] = text[:300]
    value["conclusion_complete"] = len(text) <= 300
    value["evidence"] = item.get("evidence", [])[:2]
    value["omitted_evidence"] = max(0, len(item.get("evidence", [])) - 2)
    return value


def view(plan, jobs):
    """Refresh exact facts and decisions from their maintained sources each turn."""
    state = load()
    candidates = []
    for receipt in reversed(state["receipts"]):
        fields = {}
        for key, value in receipt["facts"].items():
            if cm.size({**fields, key: value}) <= 1100:
                fields[key] = value
        omitted = receipt["omitted_facts"] + len(receipt["facts"]) - len(fields)
        candidates.append(
            {
                **receipt,
                "facts": fields,
                "omitted_facts": omitted,
                "facts_complete": receipt["facts_complete"] and omitted == 0,
            }
        )
    receipts = _bounded_items(candidates, 6, 8500)
    reviews = _bounded_items(
        (_decision_view(item) for item in reversed(plan.get("reviews", []))), 4, 3000
    )
    closed = [
        {"id": key, **route}
        for key, route in plan.get("routes", {}).items()
        if route.get("status") in {"paused", "completed"} and route.get("conclusion")
    ]
    conclusions = _bounded_items((_decision_view(item) for item in reversed(closed)), 4, 3000)
    active_all = {
        job: row["status"]
        for job, row in jobs.items()
        if row.get("status") not in {"completed", "failed", "cancelled"}
    }
    active = dict(list(active_all.items())[-32:])
    return {
        "notice": (
            "Fresh source state; use it to reconcile older summary tasks. Receipts are exact tool "
            "observations, not experiment conclusions. Check active jobs before resubmitting. "
            "Read indexed receipts from source and find older originals with recall. Read full "
            "decisions with plan show; review results in plan and save durable lessons in memory."
        ),
        "plan_revision": plan["revision"],
        "active_jobs": active,
        "omitted_active_jobs": len(active_all) - len(active),
        "recent_reviews": reviews,
        "closed_routes": conclusions,
        "recent_receipts": receipts,
        "omitted_receipts": state["evicted"] + len(state["receipts"]) - len(receipts),
        "omitted_reviews": len(plan.get("reviews", [])) - len(reviews),
        "omitted_closed_routes": len(closed) - len(conclusions),
        "source": str(state_paths.path(INDEX)),
        "plan_source_sha256": hashlib.sha256(
            json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }
