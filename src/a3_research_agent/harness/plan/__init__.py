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
                f"{key} 需要操作者处理：先 research_status(status=\"blocked\", reason=..., "
                "required_action=..., evidence=[...]) 上报，不要自行绕过或重复检查。"
            )
        return f"所有步骤都停着（{key} 等）：改变条件或新增可执行步骤，别只重复检查。"
    return "计划里没有可执行步骤：先 plan update 新增步骤或调整路线，再动手。"


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
        "reason": plan["reason"],
        "ready": ready,
        "next_action": next_action(steps, ready, review_due, routes),
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
        # One line per step keeps the whole plan visible; detail goes to the focus work.
        result["overview"] = [
            {
                "id": key,
                "status": steps[key]["status"],
                "route": steps[key].get("route", ""),
                "task": steps[key].get("task", "")[:80],
                "jobs": steps[key].get("observed_jobs", {}),
            }
            for key in ordered[:8]
        ]
        result["parallelism"] = {
            "ready_steps": len(ready),
            "active_jobs": sum(
                1
                for step in steps.values()
                for status in step["observed_jobs"].values()
                if status not in operational.TERMINAL_JOB_STATES
            ),
        }
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
            selected_routes = {step.get("route") for step in visible.values()}
            result["routes"] = {
                key: route for key, route in result["routes"].items() if key in selected_routes
            }
            result["omitted_routes"] = len(routes) - len(result["routes"])
        result["omitted_steps"] = len(steps) - len(visible)
        steps = visible
    return {**result, "steps": steps}


def dispatch(arguments):
    with plan_store.locked(plan_file()):
        return dispatch_locked(arguments)


def dispatch_locked(arguments):
    action = arguments.get("action")
    allowed = {
        "show": {"action", "id"},
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
                    "step": step,
                    "decision": decision,
                    "reason": bounded_text(arguments.get("reason", ""), 1200, "reason"),
                    "conclusion": conclusion,
                    "evidence": evidence,
                    "metrics": metrics,
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
