"""Persistent research planning and its view of observed remote work."""

from collections import Counter

from a3_research_agent.harness import operational_state as operational
from a3_research_agent.harness import paths
from a3_research_agent.harness.context import manager
from a3_research_agent.harness.plan import inconsistency
from a3_research_agent.harness.plan import store as plan_store
from a3_research_agent.harness.plan.model import (
    MAX_ACTIVE_ROUTES,
    ActiveRouteLimit,
    active_route_ids,
    validate_route_admission,
)
from a3_research_agent.harness.plan.model import STATUSES as STATUSES
from a3_research_agent.harness.plan.model import (
    bounded_list as bounded_list,
)
from a3_research_agent.harness.plan.model import (
    bounded_text as bounded_text,
)
from a3_research_agent.harness.plan.model import (
    ordered_steps as ordered_steps,
)
from a3_research_agent.harness.plan.model import (
    revise as revise,
)
from a3_research_agent.harness.plan.model import (
    truncated_text as truncated_text,
)


def plan_file():
    return paths.plan_file()


def load():
    return plan_store.load(plan_file())


def in_flight_routes(steps, jobs):
    running = operational.JOB_STATES - operational.TERMINAL_JOB_STATES
    return {
        step["route"]
        for step in steps.values()
        if step.get("route")
        and any(jobs.get(job, {}).get("status") in running for job in step["jobs"])
    }


def next_action(steps, ready, review_due, routes):
    """One sentence saying what this turn should advance.

    A weak model reads state well and decides badly; naming the next move in the
    state view is cheaper than hoping it infers the move from a list of steps.
    """
    running = {
        job: status
        for key in steps
        for job, status in (steps[key].get("observed_jobs") or {}).items()
        if status not in operational.TERMINAL_JOB_STATES and status != "unobserved"
    }
    if review_due:
        item = review_due[0]
        job, status = next(iter(item["jobs"].items()))
        route = item.get("route") or "未挂路线"
        return (
            f"先复盘 {route}：步骤 {item['step']} 的作业 {job} 已 {status}，"
            "用实测数值写 plan review（带 metrics），再决定继续还是暂停。"
        )
    if ready:
        key = ready[0]
        step = steps[key]
        criterion = (step.get("criterion") or "").strip()
        line = f"下一步做 {key}：{(step.get('task') or '')[:60]}"
        if criterion:
            line += f"（完成判据：{criterion[:60]}）"
        if running:
            job, status = next(iter(running.items()))
            line += f"；同时作业 {job} 在 {status}，出结果后复盘，不要重复提交。"
        return line.rstrip("。；;.") + "。"
    if running:
        job, status = next(iter(running.items()))
        return f"没有可开工的步骤，作业 {job} 在 {status}：等通知，不要重复提交。"
    for key, step in steps.items():
        if step["status"] not in ("pending", "active") or step["unmet_dependencies"]:
            continue
        unobserved = [
            job for job, status in step["observed_jobs"].items() if status == "unobserved"
        ]
        if unobserved and routes.get(step.get("route"), {}).get("status") not in (
            "paused",
            "completed",
        ):
            return f"先确认 {key} 的已有作业 {unobserved[0]}，用 job_status 核对后再决定是否提交。"
    blocked = [key for key, step in steps.items() if step["status"] == "blocked"]
    if blocked:
        key = blocked[0]
        reason = (steps[key].get("progress") or "").strip()
        operator_terms = (
            "权限",
            "授权",
            "操作者",
            "凭据",
            "permission",
            "credential",
            "quota",
            "8卡",
            "8 卡",
        )
        if any(term in reason.casefold() for term in operator_terms):
            return (
                f'{key} 需要操作者处理：先 research_status(status="blocked", reason=..., '
                "required_action=..., evidence=[...]) 上报，不要自行绕过或重复检查。"
            )
        return f"所有步骤都停着（{key} 等）：改变条件或新增可执行步骤，别只重复检查。"
    return "计划里没有可执行步骤：先 plan update 新增步骤或调整路线，再动手。"


def view(plan=None, *, jobs=None, compact=False):
    plan = load() if plan is None else plan
    if jobs is None:
        jobs = operational.load().get("jobs", {}) if plan["steps"] else {}
    order = ordered_steps(plan["steps"])
    ready = []
    steps = {}
    covered = dict(plan.get("reviewed_jobs", {}))
    for review in plan.get("reviews", []):
        covered.update(review["observed_jobs"])
    for key in order:
        step = plan["steps"][key]
        pending = [dep for dep in step["depends_on"] if plan["steps"][dep]["status"] != "done"]
        observed = {
            job: jobs.get(job, {}).get("status", covered.get(job, "unobserved"))
            for job in step["jobs"]
        }
        if (
            step["status"] in ("pending", "active")
            and not pending
            and plan.get("routes", {}).get(step.get("route"), {}).get("status")
            not in ("paused", "completed")
            and all(status in operational.TERMINAL_JOB_STATES for status in observed.values())
        ):
            ready.append(key)
        steps[key] = {**step, "unmet_dependencies": pending, "observed_jobs": observed}
        if not compact and observed:
            steps[key]["job_observations"] = {
                job: operational.job_observation(jobs[job]) for job in observed if job in jobs
            }
    ready.sort(
        key=lambda key: (
            steps[key].get("priority", 0),
            plan.get("routes", {}).get(steps[key].get("route"), {}).get("priority", 0),
        ),
        reverse=True,
    )
    routes = plan.get("routes", {})
    active_routes = active_route_ids(routes)
    occupied_routes = list(dict.fromkeys([*active_routes, *sorted(in_flight_routes(steps, jobs))]))
    slots = {
        "limit": MAX_ACTIVE_ROUTES,
        "occupied": len(occupied_routes),
        "available": max(0, MAX_ACTIVE_ROUTES - len(occupied_routes)),
    }
    review_due = []
    for key, step in steps.items():
        terminal = {
            job: status
            for job, status in step["observed_jobs"].items()
            if status in operational.TERMINAL_JOB_STATES and covered.get(job) != status
        }
        if terminal:
            review_due.append({"step": key, "route": step.get("route", ""), "jobs": terminal})
    active = set(active_routes[:MAX_ACTIVE_ROUTES])
    open_steps = {
        key: step
        for key, step in steps.items()
        if step["status"] not in ("done", "skipped")
        and (not step.get("route") or step["route"] in active)
    }
    startable = list(
        dict.fromkeys(
            steps[key]["route"]
            for key in ready
            if routes.get(steps[key].get("route"), {}).get("status") == "proposed"
        )
    )[: slots["available"]]
    ready_now = [key for key in ready if key in open_steps]
    running_states = operational.JOB_STATES - operational.TERMINAL_JOB_STATES
    running = [
        key
        for key, step in steps.items()
        if any(status in running_states for status in step["observed_jobs"].values())
        and (key in open_steps or step.get("route") in occupied_routes[:MAX_ACTIVE_ROUTES])
    ]
    foreground_reviews = [
        item
        for item in review_due
        if steps[item["step"]]["status"] != "skipped"
        and (item["route"] in active or (not item["route"] and item["step"] in open_steps))
    ]
    running_routes = {steps[key].get("route") for key in running}
    # Unlinked backlog from a legacy plan should not interrupt admitted work.
    actionable = [
        key
        for key in ready_now
        if steps[key]["status"] == "active"
        or (steps[key].get("route") and steps[key]["route"] not in running_routes)
        or not running
    ]
    candidates = (
        [item["step"] for item in foreground_reviews] + actionable + running + list(open_steps)
    )
    focus = candidates[:1]
    focus_ready = [key for key in focus if key in actionable]
    action = next_action(
        {key: steps[key] for key in [*focus, *running]},
        focus_ready,
        foreground_reviews,
        routes,
    )
    if not foreground_reviews and not focus_ready and startable:
        action = f"当前有路线名额，可激活独立待办 {startable[0]} 并推进其已就绪步骤。"
    result = {
        "direction": plan.get("direction", {"objective": "", "metrics": [], "gaps": []}),
        "routes": routes,
        "route_slots": slots,
        "review_due": review_due,
        "focus": focus,
        "startable_routes": startable,
        "unlinked_steps": [key for key, step in steps.items() if not step.get("route")],
        "notice": "Agent-authored plan. Job status is last observed. Verify evidence before closing.",
        "revision": plan["revision"],
        "reason": plan["reason"],
        "order": order,
        "ready": ready,
        "next_action": action,
        "counts": {
            status: sum(step["status"] == status for step in steps.values()) for status in STATUSES
        },
    }
    if not compact:
        result["reviews"] = plan.get("reviews", [])
    if compact:
        result["order"] = focus
        result["unlinked_steps"] = [key for key in focus if not steps[key].get("route")]
        # Include job/dependency signals for up to three other admitted routes.
        focus_route = steps[focus[0]].get("route") if focus else None
        result["overview"] = [
            {
                "route": route,
                "jobs": dict(
                    Counter(
                        {
                            job: status
                            for step in steps.values()
                            if step.get("route") == route
                            for job, status in step["observed_jobs"].items()
                        }.values()
                    )
                ),
                "waiting_on_dependencies": sum(
                    bool(step["unmet_dependencies"])
                    for step in open_steps.values()
                    if step.get("route") == route
                ),
                "review_due": sum(item["route"] == route for item in foreground_reviews),
            }
            for route in occupied_routes[:MAX_ACTIVE_ROUTES]
            if route != focus_route
        ][: MAX_ACTIVE_ROUTES - 1]
        result["ready"] = ready_now[: MAX_ACTIVE_ROUTES * 2]
        result["parallelism"] = {
            "ready_steps": len(ready_now),
            "active_jobs": len(
                {
                    job
                    for step in steps.values()
                    for job, status in step["observed_jobs"].items()
                    if status in operational.JOB_STATES - operational.TERMINAL_JOB_STATES
                }
            ),
        }
        visible = {key: steps[key] for key in focus}
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
        result["review_due"] = foreground_reviews[:1]
        result["omitted_reviews_due"] = len(review_due) - len(result["review_due"])
        result["omitted_steps"] = len(steps) - len(visible)
        steps = visible
    return {**result, "steps": steps}


def dispatch(arguments):
    with plan_store.locked(plan_file()):
        return dispatch_locked(arguments)


def dispatch_locked(arguments):
    action = arguments.get("action")
    allowed = {
        "show": {"action", "id", "view"},
        "update": {"action", "revision", "direction", "routes", "steps", "archive", "reason"},
        "review": {
            "action",
            "revision",
            "id",
            "decision",
            "conclusion",
            "evidence",
            "reason",
            "step",
            "metrics",
        },
    }
    if action not in allowed or arguments.keys() - allowed[action]:
        return {
            "error": "plan_validation",
            "hint": "Use only the fields for show, update or review; direction.objective holds the objective",
        }
    plan = load()
    plan_store.snapshot(plan_file(), paths.plan_snapshots_root())
    if action == "show":
        key = arguments.get("id")
        style = arguments.get("view", "all")
        if style not in ("all", "focus") or (style == "focus" and key is not None):
            return {"error": "plan_validation", "hint": 'Use view="focus" or a step id.'}
        result = view(plan, compact=style == "focus")
        if key is not None:
            if not isinstance(key, str) or key not in result["steps"]:
                return {"error": "unknown_plan_step"}
            result["steps"] = {key: result["steps"][key]}
            result["order"] = [key]
            route = result["steps"][key].get("route")
            result["routes"] = {
                name: item for name, item in result["routes"].items() if name == route
            }
            result["reviews"] = [item for item in result["reviews"] if item["route"] == route]
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
    review_warnings = []
    observed = operational.load().get("jobs", {})
    previous_running = in_flight_routes(plan["steps"], observed)
    try:
        if action == "review":
            key = arguments.get("id")
            if key not in plan.get("routes", {}):
                raise ValueError("Review requires an existing route id")
            decision = arguments.get("decision")
            if decision not in ("continue", "pause", "complete"):
                raise ValueError("decision must be continue, pause or complete")
            # Keep the review comparable and small: the plan archives the full text.
            conclusion = truncated_text(
                arguments.get("conclusion", ""), 600, "conclusion", required=True
            )
            evidence = bounded_list(
                arguments.get("evidence", []),
                20,
                lambda x: bool(x.strip()) and len(x) <= 600,
                "review evidence",
            )
            if not evidence:
                raise ValueError("Review requires evidence references")
            step = arguments.get("step")
            if step is not None:
                if not isinstance(step, str) or step not in plan["steps"]:
                    raise ValueError("Review step must identify a step in this plan")
                if plan["steps"][step].get("route") != key:
                    raise ValueError("Review step belongs to a different route")
            metrics = arguments.get("metrics", [])
            if (
                not isinstance(metrics, list)
                or len(metrics) > 8
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 160
                    for item in metrics
                )
            ):
                raise ValueError("metrics must be up to 8 short measurement strings")
            if decision == "continue" and not metrics:
                raise ValueError(
                    "Continuing a route requires at least one measured value in metrics"
                )
            if any(not any(char.isdigit() for char in item) for item in metrics):
                raise ValueError("Each metric must contain the observed value")
            jobs = {
                job: observed.get(job, {}).get("status", "unobserved")
                for step_id, item in plan["steps"].items()
                if item.get("route") == key and (step is None or step_id == step)
                for job in item["jobs"]
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
                    "step": step,
                    "decision": decision,
                    "reason": truncated_text(arguments.get("reason", ""), 400, "reason"),
                    "conclusion": conclusion,
                    "evidence": evidence,
                    "metrics": metrics,
                    "observed_jobs": jobs,
                },
            ]
            proposed["reviews"] = proposed["reviews"][-20:]
            criterion = None
            if step:
                criterion = plan["steps"][step].get("criterion")
            review_warnings = inconsistency.warnings(
                plan, key, conclusion, metrics, criterion=criterion
            )
        else:
            proposed = revise(plan, arguments)
        validate_route_admission(
            plan["routes"],
            proposed["routes"],
            previous_running,
            in_flight_routes(proposed["steps"], observed),
        )
    except ActiveRouteLimit as error:
        return {
            "error": "active_route_limit",
            "hint": str(error),
            "occupied_routes": list(
                dict.fromkeys([*active_route_ids(plan["routes"]), *sorted(previous_running)])
            ),
        }
    except ValueError as error:
        return {"error": "plan_validation", "hint": str(error)}
    if proposed != plan:
        if plan["revision"]:
            manager.archive(plan, "plan-revision")
        proposed["revision"] += 1
        plan_store.save(plan_file(), proposed, paths.plan_snapshots_root())
        plan = proposed
    result = view(plan, compact=True)
    if action == "review" and review_warnings:
        result["warnings"] = review_warnings
        result["hint"] = "Re-read the recorded measurements before relying on the cited number."
    return result
