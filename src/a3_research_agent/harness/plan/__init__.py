"""Persistent research planning and its view of observed remote work."""

from a3_research_agent.harness import operational_state as operational
from a3_research_agent.harness import paths
from a3_research_agent.harness.context import manager
from a3_research_agent.harness.plan import store as plan_store
from a3_research_agent.harness.plan.model import MAX_STEPS as MAX_STEPS
from a3_research_agent.harness.plan.model import STATUSES as STATUSES
from a3_research_agent.harness.plan.model import bounded_list, bounded_text, revise


def plan_file():
    return paths.plan_file()


def load():
    return plan_store.load(plan_file())


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
            and plan.get("routes", {}).get(step.get("route"), {}).get("status")
            not in ("paused", "completed")
            and all(
                status in operational.TERMINAL_JOB_STATES or status == "unobserved"
                for status in observed.values()
            )
        ):
            ready.append(key)
        steps[key] = {**step, "unmet_dependencies": pending, "observed_jobs": observed}
    ready.sort(
        key=lambda key: (
            steps[key].get("priority", 0),
            plan.get("routes", {}).get(steps[key].get("route"), {}).get("priority", 0),
        ),
        reverse=True,
    )
    routes = plan.get("routes", {})
    covered = dict(plan.get("reviewed_jobs", {}))
    for review in plan.get("reviews", []):
        covered.update(review["observed_jobs"])
    review_due = []
    for key, step in steps.items():
        terminal = {
            job: status
            for job, status in step["observed_jobs"].items()
            if status in operational.TERMINAL_JOB_STATES and covered.get(job) != status
        }
        if terminal:
            review_due.append({"step": key, "route": step.get("route", ""), "jobs": terminal})
    result = {
        "direction": plan.get("direction", {"objective": "", "metrics": [], "gaps": []}),
        "routes": routes,
        "review_due": review_due,
        "focus": ready[:1],
        "unlinked_steps": [key for key, step in steps.items() if not step.get("route")],
        "notice": "Agent-authored plan. Job status is last observed. Verify evidence before closing.",
        "revision": plan["revision"],
        "goal": plan["goal"],
        "reason": plan["reason"],
        "ready": ready,
        "counts": {
            status: sum(step["status"] == status for step in steps.values()) for status in STATUSES
        },
    }
    if not compact:
        result["reviews"] = plan.get("reviews", [])
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
        selected_routes = {step.get("route") for step in visible.values()}
        result["direction"] = {
            "objective": result["direction"]["objective"][:500],
            "metrics": [text[:180] for text in result["direction"]["metrics"][:4]],
            "gaps": [text[:180] for text in result["direction"]["gaps"][:4]],
        }
        result["routes"] = {
            key: {
                field: value[:300] if isinstance(value, str) else value
                for field, value in route.items()
                if field
                in (
                    "question",
                    "hypothesis",
                    "experiment",
                    "decision_rule",
                    "status",
                    "priority",
                    "conclusion",
                )
            }
            for key, route in routes.items()
            if key in selected_routes
        }
        result["omitted_routes"] = len(routes) - len(result["routes"])
        result["review_due"] = review_due[:5]
        result["omitted_reviews_due"] = len(review_due) - len(result["review_due"])
        while len(visible) > 1 and manager.size({**result, "steps": visible}) > 5500:
            del visible[next(reversed(visible))]
        result["omitted_steps"] = len(steps) - len(visible)
        steps = visible
    return {**result, "steps": steps}


def dispatch(arguments):
    with plan_store.locked(plan_file()):
        return dispatch_locked(arguments)


def dispatch_locked(arguments):
    action = arguments.get("action")
    plan = load()
    plan_store.snapshot(plan_file(), paths.plan_snapshots_root())
    if action == "show":
        result = view(plan)
        key = arguments.get("id")
        if key is not None:
            if not isinstance(key, str) or key not in result["steps"]:
                return {"error": "unknown_plan_step"}
            result["steps"] = {key: result["steps"][key]}
        return result
    if action not in ("update", "review"):
        return {"error": "plan_validation", "hint": "Use show, update or review"}
    revision = arguments.get("revision")
    if type(revision) is not int or revision != plan["revision"]:
        return {
            "error": "stale_plan_revision",
            "revision": plan["revision"],
            "hint": "Show the plan first",
        }
    try:
        if action == "review":
            key = arguments.get("id")
            if key not in plan.get("routes", {}):
                raise ValueError("Review requires an existing route id")
            decision = arguments.get("decision")
            if decision not in ("continue", "pause", "complete"):
                raise ValueError("decision must be continue, pause or complete")
            conclusion = bounded_text(
                arguments.get("conclusion", ""), 1200, "conclusion", required=True
            )
            evidence = bounded_list(
                arguments.get("evidence", []),
                20,
                lambda x: bool(x.strip()) and len(x) <= 600,
                "review evidence",
            )
            if not evidence:
                raise ValueError("Review requires evidence references")
            observed = operational.load().get("jobs", {})
            jobs = {
                job: observed.get(job, {}).get("status", "unobserved")
                for step in plan["steps"].values()
                if step.get("route") == key
                for job in step["jobs"]
                if observed.get(job, {}).get("status") in operational.TERMINAL_JOB_STATES
            }
            proposed = {**plan, "routes": {k: dict(v) for k, v in plan["routes"].items()}}
            proposed["routes"][key].update(
                status={"continue": "active", "pause": "paused", "complete": "completed"}[decision],
                conclusion=conclusion,
                evidence=evidence,
            )
            proposed["reviewed_jobs"] = {**plan.get("reviewed_jobs", {}), **jobs}
            proposed["reviews"] = [
                *plan.get("reviews", []),
                {
                    "revision": plan["revision"] + 1,
                    "route": key,
                    "decision": decision,
                    "reason": bounded_text(arguments.get("reason", ""), 1200, "reason"),
                    "conclusion": conclusion,
                    "evidence": evidence,
                    "observed_jobs": jobs,
                },
            ]
            proposed["reviews"] = proposed["reviews"][-20:]
        else:
            proposed = revise(plan, arguments)
    except ValueError as error:
        return {"error": "plan_validation", "hint": str(error)}
    if proposed != plan:
        if plan["revision"]:
            manager.archive(plan, "plan-revision")
        proposed["revision"] += 1
        plan_store.save(plan_file(), proposed, paths.plan_snapshots_root())
        plan = proposed
    return view(plan, compact=True)
