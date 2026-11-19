"""Validate research direction, route and execution-step patches."""

import re

STATUSES = ("pending", "active", "blocked", "done", "skipped")
ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
MAX_ACTIVE_ROUTES = 4


class ActiveRouteLimit(ValueError):
    """A plan update tried to start another route without a free slot."""


def active_route_ids(routes):
    return [key for key, route in routes.items() if route.get("status") == "active"]


def validate_route_admission(previous, routes, previous_in_flight=(), in_flight=()):
    active = set(active_route_ids(routes)).union(previous_in_flight, in_flight)
    previously_occupied = set(active_route_ids(previous)).union(previous_in_flight)
    if len(active) > MAX_ACTIVE_ROUTES and active.difference(previously_occupied):
        raise ActiveRouteLimit(
            f"At most {MAX_ACTIVE_ROUTES} routes may occupy slots. Finish or pause a route "
            "with no jobs in flight, or keep the candidate proposed."
        )


def bounded_text(value, limit, name, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return value.strip()


def truncated_text(value, limit, name, *, required=False):
    """Keep a long note by shortening it; reject only wrong types or empty required text."""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be text of at most {limit} characters")
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def bounded_list(value, limit, valid, name):
    if (
        not isinstance(value, list)
        or (limit is not None and len(value) > limit)
        or any(not isinstance(item, str) or not valid(item) for item in value)
    ):
        suffix = f"; use at most {limit} valid entries" if limit is not None else ""
        raise ValueError(f"Invalid {name}{suffix}")
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
    for name, limit in (("task", 200), ("criterion", 240), ("progress", 600)):
        # A long progress note is a record, not an invalid argument: keep it and
        # shorten it rather than rejecting the whole revision.
        step[name] = truncated_text(step[name], limit, name)
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


def ordered_steps(steps):
    """Return step IDs in dependency order, priority first among ready steps."""
    keys = list(steps)
    position = {key: index for index, key in enumerate(keys)}
    dependencies = {
        key: {dependency for dependency in step["depends_on"] if dependency in steps}
        for key, step in steps.items()
    }
    children = {key: [] for key in keys}
    for key, required in dependencies.items():
        for dependency in required:
            children[dependency].append(key)

    def priority(key):
        return (-steps[key].get("priority", 0), position[key])

    ready = [key for key in keys if not dependencies[key]]
    ready.sort(key=priority)
    ordered = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for child in children[key]:
            dependencies[child].discard(key)
            if not dependencies[child]:
                ready.append(child)
        ready.sort(key=priority)
    ordered.extend(key for key in keys if key not in ordered)
    return ordered


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


RESEARCH_LANES = ("implementation", "papers")
ROUTE_DESIGN_FIELDS = ("question", "hypothesis", "experiment", "decision_rule")


def validate_research(research):
    if not isinstance(research, dict) or research.keys() != {*RESEARCH_LANES, "design"}:
        raise ValueError("Route research requires implementation, papers and design")
    bounded_text(research["design"], 1200, "research design", required=True)
    for lane in RESEARCH_LANES:
        sources = research[lane]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 10:
            raise ValueError(f"research.{lane} requires one to ten source findings")
        for source in sources:
            if not isinstance(source, dict) or source.keys() != {"url", "finding"}:
                raise ValueError(f"Each research.{lane} source requires url and finding")
            url = bounded_text(source["url"], 4096, "research source URL", required=True)
            if not url.startswith(("https://", "http://")) or any(c.isspace() for c in url):
                raise ValueError("Use an HTTP(S) source URL returned by web_fetch")
            bounded_text(source["finding"], 1200, "research source finding", required=True)
    if {s["url"] for s in research["implementation"]} & {s["url"] for s in research["papers"]}:
        raise ValueError("Cite distinct implementation and paper sources")


def route_design_changed(previous, route):
    return any(route[field] != previous.get(field) for field in ROUTE_DESIGN_FIELDS)


def revise(plan, arguments):
    changes = arguments.get("steps", [])
    if not isinstance(changes, list):
        raise ValueError("Supply step updates as a list")  # noqa: TRY004
    direction_patch = arguments.get("direction", {})
    if not isinstance(direction_patch, dict) or direction_patch.keys() - {
        "objective",
        "metrics",
        "gaps",
    }:
        raise ValueError("direction accepts objective, metrics and gaps")
    reason = bounded_text(arguments.get("reason", ""), 240, "reason")
    steps = plan["steps"].copy()
    archived = bounded_list(arguments.get("archive", []), None, ID.fullmatch, "archive IDs")
    if archived and not reason:
        raise ValueError("Give a reason when archiving closed steps")
    for key in archived:
        if steps.get(key, {}).get("status") not in ("done", "skipped"):
            raise ValueError("Only done or skipped steps can be archived")
        del steps[key]
    seen = set()
    route_changed = bool(
        plan["revision"]
        and direction_patch.get("objective", plan["direction"]["objective"])
        != plan["direction"]["objective"]
    )
    for patch in changes:
        key = patch.get("id") if isinstance(patch, dict) else None
        if not isinstance(key, str) or not ID.fullmatch(key) or key in seen or key in archived:
            raise ValueError("Each step requires a unique ID of 1–48 letters, digits, _ or -")
        seen.add(key)
        previous = steps.get(key, {})
        step = merge_step(previous, patch)
        if previous:
            if (
                previous["status"] == step["status"] == "done"
                and step["criterion"] != previous["criterion"]
                and not set(step["evidence"]).difference(previous["evidence"])
            ):
                raise ValueError(
                    f"Reopen {key}, create a new step, or supply new evidence for the changed criterion"
                )
            route_changed |= any(
                step[field] != previous[field] for field in ("task", "criterion", "depends_on")
            )
            route_changed |= previous["status"] in ("done", "skipped") and step != previous
        steps[key] = step
    if route_changed and not reason:
        raise ValueError("Give a reason when revising direction, route or a closed step")
    validate_dependencies(steps)
    direction = {**plan["direction"], **direction_patch}
    direction["objective"] = bounded_text(direction["objective"], 1200, "objective", required=True)
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
        if patch.keys() - route.keys() - {"id", "research"}:
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
        changed_design = previous and route_design_changed(previous, route)
        if changed_design:
            if not reason:
                raise ValueError("Give reason when changing a research route")
            if "research" not in patch:
                route.pop("research", None)
        if "research" in patch:
            validate_research(route["research"])
        routes[key] = route
    validate_route_admission(plan.get("routes", {}), routes)
    if any(step.get("route") and step["route"] not in routes for step in steps.values()):
        raise ValueError("Step route must identify an existing research route")
    return {
        **plan,
        "direction": direction,
        "routes": routes,
        "reason": reason,
        "steps": steps,
    }
