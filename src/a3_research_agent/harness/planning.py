"""Workspace-local research plan and its view of observed remote work."""

import json
import re
from pathlib import Path

from a3_research_agent.harness.context import manager
from a3_research_agent.harness.memory import state as operational

PATH = Path("research-plan.json")
STATUSES = ("pending", "active", "blocked", "done", "skipped")
ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
MAX_STEPS = 20


def load():
    if not PATH.exists():
        return {"version": 1, "revision": 0, "goal": "", "reason": "", "steps": {}}
    plan = json.loads(PATH.read_text())
    if plan.get("version") != 1:
        raise ValueError("Unsupported research plan version")
    return plan


def bounded_text(value, limit, name, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return value.strip()


def bounded_list(value, limit, valid, name):
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) or not valid(item) for item in value)
    ):
        raise ValueError(f"Invalid {name}; use at most {limit} valid entries")
    return list(dict.fromkeys(value))


def merge_step(previous, changes):
    defaults = {
        "task": "",
        "criterion": "",
        "status": "pending",
        "depends_on": [],
        "jobs": [],
        "progress": "",
        "evidence": [],
    }
    if changes.keys() - defaults.keys() - {"id"}:
        raise ValueError("Unknown step field")
    step = {**defaults, **previous, **{key: value for key, value in changes.items() if key != "id"}}
    for name, limit in (("task", 200), ("criterion", 240), ("progress", 300)):
        step[name] = bounded_text(step[name], limit, name, required=name != "progress")
    step["depends_on"] = bounded_list(step["depends_on"], 8, ID.fullmatch, "dependency IDs")
    step["jobs"] = bounded_list(step["jobs"], 8, JOB_ID.fullmatch, "Controller job IDs")
    step["evidence"] = bounded_list(
        step["evidence"],
        4,
        lambda ref: bool(ref.strip()) and len(ref) <= 240,
        "evidence references",
    )
    status = step["status"]
    if status not in STATUSES:
        raise ValueError("Invalid step status")
    if status == "done" and not step["evidence"]:
        raise ValueError("A done step requires evidence for its completion criterion")
    if status in ("blocked", "skipped") and not step["progress"]:
        raise ValueError("A blocked or skipped step requires a reason in progress")
    return step


def validate_dependencies(steps):
    remaining = {key: set(step["depends_on"]) for key, step in steps.items()}
    if any(dependencies - steps.keys() for dependencies in remaining.values()):
        raise ValueError("Dependencies must identify steps in this plan")
    while remaining:
        roots = {key for key, dependencies in remaining.items() if not dependencies}
        if not roots:
            raise ValueError("Dependencies contain a cycle")
        remaining = {
            key: dependencies - roots for key, dependencies in remaining.items() if key not in roots
        }
    for step in steps.values():
        if step["status"] == "done" and any(
            steps[key]["status"] != "done" for key in step["depends_on"]
        ):
            raise ValueError("A done step requires all its dependencies to be done")


def revise(plan, arguments):
    changes = arguments.get("steps", [])
    if not isinstance(changes, list) or len(changes) > MAX_STEPS:
        raise ValueError(f"Supply at most {MAX_STEPS} step updates")
    goal = bounded_text(arguments.get("goal", plan["goal"]), 300, "goal", required=True)
    reason = bounded_text(arguments.get("reason", ""), 240, "reason")
    steps = plan["steps"].copy()
    archived = bounded_list(arguments.get("archive", []), MAX_STEPS, ID.fullmatch, "archive IDs")
    if archived and not reason:
        raise ValueError("Give a reason when archiving closed steps")
    for key in archived:
        if steps.get(key, {}).get("status") not in ("done", "skipped"):
            raise ValueError("Only done or skipped steps can be archived")
        del steps[key]
    seen = set()
    route_changed = bool(plan["revision"] and goal != plan["goal"])
    for patch in changes:
        key = patch.get("id") if isinstance(patch, dict) else None
        if not isinstance(key, str) or not ID.fullmatch(key) or key in seen or key in archived:
            raise ValueError("Each step requires a unique ID of 1–48 letters, digits, _ or -")
        seen.add(key)
        previous = steps.get(key, {})
        step = merge_step(previous, patch)
        if previous:
            route_changed |= any(
                step[field] != previous[field] for field in ("task", "criterion", "depends_on")
            )
            route_changed |= previous["status"] in ("done", "skipped") and step != previous
        steps[key] = step
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"A plan must contain 1–{MAX_STEPS} steps")
    if route_changed and not reason:
        raise ValueError("Give a reason when revising the goal, route or a closed step")
    validate_dependencies(steps)
    return {**plan, "goal": goal, "reason": reason, "steps": steps}


def view(plan=None, *, jobs=None, compact=False):
    plan = load() if plan is None else plan
    if jobs is None:
        jobs = operational.load().get("jobs", {}) if plan["steps"] else {}
    ready = []
    steps = {}
    for key, step in plan["steps"].items():
        pending = [dep for dep in step["depends_on"] if plan["steps"][dep]["status"] != "done"]
        observed = {job: jobs.get(job, {}).get("status", "unobserved") for job in step["jobs"]}
        if (
            step["status"] in ("pending", "active")
            and not pending
            and all(
                status in operational.TERMINAL_JOB_STATES or status == "unobserved"
                for status in observed.values()
            )
        ):
            ready.append(key)
        steps[key] = {**step, "unmet_dependencies": pending, "observed_jobs": observed}
    result = {
        "notice": "Agent-authored plan. Job status is last observed. Verify evidence before closing.",
        "revision": plan["revision"],
        "goal": plan["goal"],
        "reason": plan["reason"],
        "ready": ready,
        "counts": {
            status: sum(step["status"] == status for step in steps.values()) for status in STATUSES
        },
    }
    if compact:
        ordered = ready + [
            key for key, step in steps.items() if key not in ready and step["status"] == "active"
        ]
        ordered += [
            key
            for key, step in steps.items()
            if key not in ordered and step["status"] in ("pending", "blocked")
        ]
        visible = {key: steps[key] for key in ordered[:5]}
        while visible and manager.size({**result, "steps": visible}) > 5500:
            del visible[next(reversed(visible))]
        result["omitted_steps"] = len(steps) - len(visible)
        steps = visible
    return {**result, "steps": steps}


def dispatch(arguments):
    action = arguments.get("action")
    plan = load()
    if action == "show":
        result = view(plan)
        key = arguments.get("id")
        if key is not None:
            if not isinstance(key, str) or key not in result["steps"]:
                return {"error": "unknown_plan_step"}
            result["steps"] = {key: result["steps"][key]}
        return result
    if action != "update":
        return {"error": "plan_validation", "hint": "Use show or update"}
    revision = arguments.get("revision")
    if type(revision) is not int or revision != plan["revision"]:
        return {
            "error": "stale_plan_revision",
            "revision": plan["revision"],
            "hint": "Show the plan first",
        }
    try:
        proposed = revise(plan, arguments)
    except ValueError as error:
        return {"error": "plan_validation", "hint": str(error)}
    if proposed["goal"] != plan["goal"] or proposed["steps"] != plan["steps"]:
        if plan["revision"]:
            manager.archive(plan, "plan-revision")
        proposed["revision"] += 1
        manager.atomic(PATH, json.dumps(proposed, ensure_ascii=False))
        plan = proposed
    return view(plan, compact=True)
