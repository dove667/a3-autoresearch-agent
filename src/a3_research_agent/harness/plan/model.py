"""Validate research direction, route and execution-step patches."""

import re

STATUSES = ("pending", "active", "blocked", "done", "skipped")
ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
MAX_STEPS = 20


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
        "route": "",
        "priority": 0,
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
    step["route"] = bounded_text(step["route"], 48, "route")
    if type(step["priority"]) is not int or not 0 <= step["priority"] <= 100:
        raise ValueError("priority must be an integer from 0 to 100")
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
    goal = bounded_text(
        arguments.get("goal", arguments.get("direction", {}).get("objective", plan["goal"])),
        1200,
        "goal",
        required=True,
    )
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
    direction = {**plan.get("direction", {"objective": "", "metrics": [], "gaps": []})}
    patch = arguments.get("direction", {})
    if not isinstance(patch, dict) or patch.keys() - direction.keys():
        raise ValueError("direction accepts objective, metrics and gaps")
    direction.update(patch)
    direction["objective"] = bounded_text(direction["objective"], 1200, "objective")
    for field in ("metrics", "gaps"):
        direction[field] = bounded_list(
            direction[field], 20, lambda x: bool(x.strip()) and len(x) <= 600, field
        )
    routes = {key: dict(value) for key, value in plan.get("routes", {}).items()}
    patches = arguments.get("routes", [])
    if not isinstance(patches, list):
        raise ValueError("routes must be a list of patches")  # noqa: TRY004
    for patch in patches:
        if not isinstance(patch, dict):
            raise ValueError("Each route must be an object")  # noqa: TRY004
        key = patch.get("id", "")
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError("Each route requires a stable ID")
        previous = routes.get(key, {})
        route = {
            "question": "",
            "hypothesis": "",
            "experiment": "",
            "decision_rule": "",
            "status": "proposed",
            "priority": 0,
            "conclusion": "",
            "evidence": [],
            **previous,
        }
        if patch.keys() - route.keys() - {"id"}:
            raise ValueError("Unknown route field")
        route.update({k: v for k, v in patch.items() if k != "id"})
        for field in ("question", "hypothesis", "experiment", "decision_rule", "conclusion"):
            route[field] = bounded_text(route[field], 1200, field, required=field != "conclusion")
        if route["status"] not in ("proposed", "active", "paused", "completed"):
            raise ValueError("Invalid route status")
        if type(route["priority"]) is not int or not 0 <= route["priority"] <= 100:
            raise ValueError("priority must be an integer from 0 to 100")
        route["evidence"] = bounded_list(
            route["evidence"], 20, lambda x: bool(x.strip()) and len(x) <= 600, "route evidence"
        )
        if route["status"] == "completed" and not (route["conclusion"] and route["evidence"]):
            raise ValueError("Completed routes require conclusion and evidence")
        if (
            previous
            and any(route[f] != previous[f] for f in ("hypothesis", "experiment", "decision_rule"))
            and not reason
        ):
            raise ValueError("Give reason when changing a research route")
        routes[key] = route
    if any(step.get("route") and step["route"] not in routes for step in steps.values()):
        raise ValueError("Step route must identify an existing research route")
    return {
        **plan,
        "goal": goal,
        "direction": direction,
        "routes": routes,
        "reason": reason,
        "steps": steps,
    }
