"""Prepare seed shards and collect their results through the existing job queue."""

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager

from . import benchmark_report, job_preparation, state_paths, waiting
from .context.manager import atomic
from .tool_contracts import ToolInputError, fail, text

GROUP_ID = re.compile(r"benchmark-[0-9a-f]{32}")
TERMINAL = {"completed", "failed", "cancelled"}


@contextmanager
def locked_group(group_id):
    directory = state_paths.path("benchmark-groups")
    directory.mkdir(parents=True, exist_ok=True)
    path = state_paths.path(f"benchmark-groups/{group_id}.json")
    lock = state_paths.path(f"benchmark-groups/{group_id}.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield path


def prepare_group(args, request, workspace, prepare_shard):
    if {"episodes", "seed_start"} & args.keys():
        fail("invalid_seeds", "Use seeds or episodes/seed_start.")
    seeds = args["seeds"]
    if (
        not isinstance(seeds, list)
        or not 1 <= len(seeds) <= 20
        or any(type(seed) is not int or not 0 <= seed < 2**31 for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        fail("invalid_seeds", "Give 1–20 distinct integer seeds in 0..2147483647.")
    cell = {key: value for key, value in args.items() if key != "seeds"}
    if "name" in cell:
        cell["name"] = text(cell["name"], "name", 120).strip()
    encoded = json.dumps({**cell, "seeds": sorted(seeds)}, sort_keys=True)
    group_id = "benchmark-" + hashlib.sha256(encoded.encode()).hexdigest()[:32]
    with locked_group(group_id) as path:
        shards = json.loads(path.read_text()) if path.exists() else []
        for seed in sorted(seeds)[len(shards) :]:
            name = cell.get("name") or f"{cell['task']}-{cell.get('profile', 'basic')}"
            shard_args = {
                **cell,
                "episodes": 1,
                "seed_start": seed,
                "name": f"{name[:100]} seed {seed}",
            }
            prepared = prepare_shard(shard_args, request, workspace, group_id=group_id)
            shard = {
                "seed": seed,
                **{
                    key: prepared[key]
                    for key in ("prepared_id", "preflight_job_id", "report", "receipt")
                    if key in prepared
                },
                "status": "preflight"
                if prepared.get("preflight_job_id")
                else "preflight_unavailable",
            }
            if shard["status"] != "preflight":
                shard["error"] = prepared.get("error", shard["status"])
            shards.append(shard)
            atomic(path, json.dumps(shards))
        return group_view(group_id, shards)


def group_view(group_id, shards):
    rows = [shard["result"] for shard in shards if "result" in shard]
    pending = {
        shard["seed"]
        for shard in shards
        if waiting_id(shard) or shard["status"] == "submission_pending"
    }
    failed = [
        shard["seed"] for shard in shards if shard["seed"] not in pending and "result" not in shard
    ]
    protocols = [
        {
            key: value
            for key, value in shard["protocol"].items()
            if key not in {"seed_start", "episodes"}
        }
        for shard in shards
        if "protocol" in shard
    ]
    comparable = not protocols or all(protocol == protocols[0] for protocol in protocols)
    return {
        "group_id": group_id,
        "status": "active"
        if pending
        else "incomplete"
        if failed or not comparable
        else "completed",
        "shards": [
            {key: value for key, value in shard.items() if key not in {"result", "protocol"}}
            for shard in shards
        ],
        "outcomes": rows,
        "totals": {
            "scored_seeds": len(rows),
            "expected_seeds": len(shards),
            "raw_score": sum(row["score_raw"] for row in rows),
            "capped_score": sum(row["score_capped"] for row in rows),
            "maximum": sum(row["max_score"] for row in rows),
            "passes": sum(row["success"] for row in rows),
        }
        if comparable
        else None,
        **({"protocol": protocols[0]} if comparable and protocols else {}),
        **(
            {"warning": "Effective protocol differs across shards; compare individual receipts."}
            if not comparable
            else {}
        ),
        "failed_seeds": failed,
    }


def waiting_id(shard):
    key = {"preflight": "preflight_job_id", "running": "job_id"}.get(shard["status"])
    return shard.get(key) if key else None


def apply_status(shard, state, request, workspace):
    phase = shard["status"]
    status = state.get("status")
    if phase != "submission_pending" and status not in TERMINAL:
        return
    if phase in {"preflight", "submission_pending"}:
        if phase == "preflight" and (status != "completed" or state.get("exit_code") != 0):
            shard.update(status="preflight_failed", error=status)
            return
        result = job_preparation.submit(shard["prepared_id"], request, workspace)
        if job_id := job_preparation.submitted_id(result):
            shard.update(status="running", job_id=job_id)
        elif result.get("status") == "resource_busy":
            shard.update(status="submission_pending")
        else:
            shard.update(
                status="submission_unconfirmed", error=result.get("error", "submission_unconfirmed")
            )
        return
    if status != "completed" or state.get("exit_code") != 0:
        shard.update(status=status, error=state.get("error") or "benchmark_job_failed")
        return
    try:
        receipt = state.get("benchmark") or benchmark_report.read_receipt(
            request, shard["job_id"], shard["receipt"]
        )
        episodes = receipt["episodes"]
        if len(episodes) != 1 or episodes[0]["seed"] != shard["seed"]:
            fail("seed_mismatch", "Check the benchmark receipt against the requested seed.")
    except ToolInputError as error:
        shard.update(status="receipt_unavailable", error=error.code)
        return
    outcome = {key: value for key, value in episodes[0].items() if key != "trace_events"}
    shard.update(status="completed", result={**outcome, "job_id": shard["job_id"]})
    shard["protocol"] = receipt["protocol"]


def advance_group(group_id, request, workspace, timeout=0, *, cursor=None):
    if not isinstance(group_id, str) or not GROUP_ID.fullmatch(group_id):
        fail("invalid_group_id", "Use the group_id returned by benchmark(seeds=...).")
    with locked_group(group_id) as path:
        if not path.exists():
            fail("unknown_group", "Use a benchmark group created in this run.")
        shards = json.loads(path.read_text())
        active = [identifier for shard in shards if (identifier := waiting_id(shard))]
        view = request("POST", "/jobs/observe", {"job_ids": active}) if active else {}
        if not isinstance(view, dict):
            view = {}
        observed = {row["job_id"]: row for row in view.get("jobs", []) if isinstance(row, dict)}
        changed = False
        for shard in shards:
            identifier = waiting_id(shard)
            phase = shard["status"]
            if identifier or phase == "submission_pending":
                apply_status(shard, observed.get(identifier, {}), request, workspace)
                if phase != shard["status"]:
                    changed = True
                    atomic(path, json.dumps(shards))
        active = [identifier for shard in shards if (identifier := waiting_id(shard))]
        if timeout and active and not changed:
            signal = waiting.wait_for_jobs(request, active, timeout_seconds=timeout, cursor=cursor)
            for shard in shards:
                if state := signal.get("changes", {}).get(f"/jobs/{waiting_id(shard)}"):
                    apply_status(shard, state, request, workspace)
                    atomic(path, json.dumps(shards))
            return {**group_view(group_id, shards), "wait_reason": signal["reason"]}
        return {
            **group_view(group_id, shards),
            **({"observation": view["observation"]} if "observation" in view else {}),
        }
