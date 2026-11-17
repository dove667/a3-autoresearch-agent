"""Assemble one human-facing, read-only view of the research state."""

import time
from datetime import datetime
from pathlib import Path

from a3_research_agent.harness import plan as planning

from . import knowledge
from .readers import read_json, read_json_lines, tail_lines
from .runs import (
    STATE_LABELS,
    TERMINAL_JOB_STATES,
    job_meta,
    read_run,
    run_ids,
    select_run,
    summarize_run,
)

STEP_LABELS = {
    "pending": "待开始",
    "active": "进行中",
    "blocked": "受阻",
    "done": "已完成",
    "skipped": "已跳过",
}

ROUTE_LABELS = {
    "proposed": "待验证",
    "active": "进行中",
    "paused": "已暂停",
    "completed": "已完成",
}

DECISION_LABELS = {"continue": "继续", "pause": "暂停", "complete": "完成"}

STATE_DETAILS = {
    "not_started": "还没有研究运行。",
    "starting": "正在准备运行环境。",
    "running": "正在推理或执行步骤。",
    "waiting_jobs": "等待远程作业结束。",
    "waiting_event": "等待人工反馈或输入更新。",
    "context_waiting": "正在整理上下文。",
    "context_blocked": "上下文服务暂时不可用。",
    "blocked": "需要人工处理。",
    "suspended": "Host 已暂停，现场保留。",
    "stopped": "研究进程已结束。",
    "interrupted": "进程已退出，Host 尚未归档。",
    "unknown": "无法确认本机进程状态。",
}


def _clock(now):
    return time.time() if now is None else now


def _short(text, limit=48):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _empty_plan():
    return {
        "revision": 0,
        "objective": "",
        "metrics": [],
        "gaps": [],
        "routes": [],
        "steps": [],
        "order": [],
        "reviews": [],
        "counts": {key: 0 for key in STEP_LABELS},
        "progress": {"done": 0, "total": 0, "percent": 0},
        "focus": None,
        "ready": [],
        "review_due": [],
        "next_hint": "计划里还没有可执行步骤。",
        "unlinked_steps": [],
    }


def plan_payload(plan_file, jobs):
    """Present the maintained plan without exposing its storage details."""
    plan = read_json(plan_file, None)
    if not isinstance(plan, dict) or plan.get("version") != 2:
        return _empty_plan()
    plan = {
        "reason": "",
        "routes": {},
        "steps": {},
        "reviews": [],
        "reviewed_jobs": {},
        **plan,
    }
    try:
        observed = {job: {"status": row.get("status")} for job, row in jobs.items()}
        view = planning.view(plan, jobs=observed, compact=False)
    except Exception:  # noqa: BLE001 - a readable partial plan still helps the operator
        view = {
            "focus": [],
            "ready": [],
            "review_due": [],
            "steps": plan.get("steps", {}),
            "counts": {},
            "next_action": "",
        }
    raw_steps = {
        key: step
        for key, step in (plan.get("steps") or {}).items()
        if isinstance(step, dict)
    }
    raw_routes = plan.get("routes") if isinstance(plan.get("routes"), dict) else {}
    views = view.get("steps") if isinstance(view.get("steps"), dict) else raw_steps
    ordered = view.get("order") if isinstance(view.get("order"), list) else []
    order = [key for key in ordered if key in raw_steps]
    order += [key for key in raw_steps if key not in order]

    routes = []
    for key, route in raw_routes.items():
        if not isinstance(route, dict):
            continue
        route_pairs = [
            (step_id, raw_steps[step_id])
            for step_id in order
            if raw_steps[step_id].get("route") == key
        ]
        route_steps = [step for _, step in route_pairs]
        done = sum(step.get("status") == "done" for step in route_steps)
        active = next(
            (pair for pair in route_pairs if pair[1].get("status") == "active"),
            next((pair for pair in route_pairs if pair[1].get("status") == "pending"), None),
        )
        routes.append(
            {
                "id": key,
                "status": route.get("status", "proposed"),
                "status_label": ROUTE_LABELS.get(route.get("status"), "待验证"),
                "priority": route.get("priority", 0),
                "question": route.get("question") or "",
                "hypothesis": route.get("hypothesis") or "",
                "experiment": route.get("experiment") or "",
                "decision_rule": route.get("decision_rule") or "",
                "conclusion": route.get("conclusion") or "",
                "steps": [step_id for step_id, _ in route_pairs],
                "progress": {
                    "done": done,
                    "total": len(route_steps),
                    "percent": round(done / len(route_steps) * 100) if route_steps else 0,
                },
                "active_step": active[0] if active else None,
                "active_task": active[1].get("task") if active else "",
            }
        )
    routes.sort(
        key=lambda route: (
            {"active": 0, "proposed": 1, "paused": 2, "completed": 3}.get(route["status"], 4),
            -route["priority"],
        )
    )

    focus = view.get("focus") or []
    focus_id = focus[0] if focus else None
    steps = []
    for key in order:
        step = raw_steps[key]
        observed = views.get(key) or {}
        status = step.get("status", "pending")
        job_rows = []
        for job in step.get("jobs") or []:
            if not isinstance(job, str):
                continue
            state = (jobs.get(job) or {}).get("status", "unobserved")
            meta = job_meta(state)
            job_rows.append(
                {
                    "id": job,
                    "short": job[:8],
                    "status": state,
                    "status_label": meta["label"],
                    "tone": meta["tone"],
                }
            )
        steps.append(
            {
                "id": key,
                "route": step.get("route") or "",
                "route_question": (raw_routes.get(step.get("route")) or {}).get("question", ""),
                "task": step.get("task") or "",
                "criterion": step.get("criterion") or "",
                "status": status,
                "status_label": STEP_LABELS.get(status, status),
                "priority": step.get("priority", 0),
                "progress": step.get("progress") or "",
                "jobs": job_rows,
                "evidence_count": len(step.get("evidence") or []),
                "depends_on": [dep for dep in step.get("depends_on") or [] if isinstance(dep, str)],
                "unmet_dependencies": [
                    dep for dep in observed.get("unmet_dependencies") or [] if isinstance(dep, str)
                ],
                "observed_jobs": observed.get("observed_jobs") or {},
                "is_focus": key == focus_id,
            }
        )

    counts = {key: sum(step["status"] == key for step in steps) for key in STEP_LABELS}
    total = len(steps)
    done = counts["done"]

    review_due = []
    for item in view.get("review_due") or []:
        if not isinstance(item, dict):
            continue
        step_id = item.get("step")
        jobs_due = item.get("jobs") if isinstance(item.get("jobs"), dict) else {}
        for job, status in jobs_due.items():
            step = raw_steps.get(step_id) or {}
            route = raw_routes.get(item.get("route")) or {}
            review_due.append(
                {
                    "step": step_id,
                    "task": step.get("task") or "",
                    "route": item.get("route") or "",
                    "route_question": route.get("question") or "",
                    "job": job,
                    "short": job[:8],
                    "status": status,
                    "status_label": job_meta(status)["label"],
                }
            )

    reviews = []
    for review in reversed(plan.get("reviews") or []):
        if not isinstance(review, dict):
            continue
        route_id = review.get("route")
        route = raw_routes.get(route_id) or {}
        step_id = review.get("step")
        step = raw_steps.get(step_id) or {}
        decision = review.get("decision")
        reviews.append(
            {
                "revision": review.get("revision"),
                "route": route_id,
                "route_question": route.get("question") or "",
                "step": step_id,
                "step_task": step.get("task") or "",
                "decision": decision,
                "decision_label": DECISION_LABELS.get(decision, decision or ""),
                "conclusion": review.get("conclusion") or "",
                "metrics": [
                    metric for metric in review.get("metrics") or [] if isinstance(metric, str)
                ],
                "evidence_count": len(review.get("evidence") or []),
                "jobs": list((review.get("observed_jobs") or {}).keys()),
            }
        )

    active_jobs = [
        job for job, row in jobs.items() if row.get("status") not in TERMINAL_JOB_STATES
    ]
    focus_step = next((step for step in steps if step["is_focus"]), None)
    if review_due:
        next_hint = f"有 {len(review_due)} 个作业已经结束，等待写入复盘。"
    elif focus_step:
        next_hint = f"下一步：{_short(focus_step['task'], 60)}"
    elif active_jobs:
        next_hint = f"等待 {len(active_jobs)} 个远程作业完成。"
    elif counts["blocked"]:
        next_hint = "有步骤停在阻塞状态，需要改变条件或人工处理。"
    else:
        next_hint = "计划里还没有可执行步骤。"

    return {
        "revision": plan.get("revision", 0),
        "objective": (plan.get("direction") or {}).get("objective") or "",
        "metrics": [
            metric
            for metric in (plan.get("direction") or {}).get("metrics") or []
            if isinstance(metric, str)
        ],
        "gaps": [
            gap for gap in (plan.get("direction") or {}).get("gaps") or [] if isinstance(gap, str)
        ],
        "routes": routes,
        "steps": steps,
        "order": order,
        "reviews": reviews,
        "counts": counts,
        "progress": {
            "done": done,
            "total": total,
            "percent": round(done / total * 100) if total else 0,
        },
        "focus": focus_step,
        "ready": [key for key in view.get("ready") or [] if isinstance(key, str)],
        "review_due": review_due[:8],
        "next_hint": next_hint,
        "unlinked_steps": [
            step["id"] for step in steps if not step["route"]
        ],
    }


def _activity_events(run_path, run, plan):
    job_step = {}
    for step in plan.get("steps") or []:
        for job in step.get("jobs") or []:
            job_step[job["id"]] = step
    items = []
    ledger = run_path / "conversation/research-ledger.jsonl"
    for event in read_json_lines(ledger, max_bytes=1_000_000):
        kind = event.get("event")
        ts = event.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        title, detail, tone = "研究记录", "", "idle"
        job = event.get("job_id")
        step = job_step.get(job)
        subject = step.get("task") if step else ""
        if kind == "job_prepare":
            title, tone = "准备远程作业", "wait"
            detail = subject or "提交前的 CPU 预检"
        elif kind == "job_submit":
            title, tone = "提交远程作业", "active"
            detail = subject
        elif kind == "job_status":
            status = event.get("status")
            if status == "completed":
                title, tone = "远程作业完成", "done"
            elif status == "failed":
                title, tone = "远程作业失败", "alert"
            elif status == "cancelled":
                title, tone = "远程作业取消", "idle"
            else:
                title, tone = "确认远程作业状态", "wait"
            detail = subject or "未绑定到计划步骤"
        elif kind == "job_wait":
            reason = event.get("reason")
            if reason == "state_changed":
                title, tone = "收到作业完成通知", "done"
            elif reason == "timeout":
                title, tone = "等待作业超时，继续观察", "wait"
            elif reason == "notification":
                title, tone = "收到人工通知", "active"
            elif reason == "inputs_changed":
                title, tone = "研究输入发生变化", "active"
            else:
                title, tone = "等待远程作业", "wait"
            detail = subject
        elif kind == "compaction":
            title, tone = "整理研究上下文", "wait"
            detail = "对话压缩完成，继续下一步"
        else:
            title = "研究记录"
        items.append(
            {
                "ts": float(ts),
                "title": title,
                "detail": _short(detail, 90),
                "tone": tone,
            }
        )
    if run and run.get("decisions"):
        last = run["decisions"][-1]
        items.append(
            {
                "ts": last,
                "title": "完成一次研究决策",
                "detail": "模型读取当前状态并选择下一步",
                "tone": "active",
            }
        )
    unique = {}
    for item in items:
        unique[(item["ts"], item["title"], item["detail"])] = item
    return sorted(unique.values(), key=lambda item: item["ts"], reverse=True)[:14]


def _pulse(decisions, now, started_at):
    hours = 12
    start = now - hours * 3600
    buckets = [0] * 24
    for ts in decisions:
        if ts < start:
            continue
        index = min(23, int((ts - start) / (hours * 3600) * 24))
        buckets[index] += 1
    return {
        "hours": hours,
        "start": start,
        "end": now,
        "buckets": buckets,
        "total": sum(buckets),
        "started_at": started_at,
    }


def _agent(run, plan, now):
    if run is None:
        state = "not_started"
    else:
        state = run.get("state", "unknown")
    label, tone = STATE_LABELS.get(state, STATE_LABELS["unknown"])
    focus = plan.get("focus")
    active_jobs = [
        job
        for job, row in (run or {}).get("jobs", {}).items()
        if row.get("status") not in TERMINAL_JOB_STATES
    ]
    if state == "not_started":
        headline = "还没有启动研究"
    elif state == "interrupted":
        headline = "研究进程已中断"
    elif state == "blocked":
        headline = "研究遇到阻塞"
    elif state == "stopped":
        headline = "这次研究已经停止"
    elif state == "suspended":
        headline = "研究已暂停"
    elif state == "waiting_jobs":
        waiting = len(active_jobs) or len((run or {}).get("pending", {}).get("jobs", []))
        headline = f"等待 {waiting} 个远程作业"
    elif state == "waiting_event":
        headline = "Agent 在等待新事件"
    elif state in {"context_waiting", "context_blocked"}:
        headline = "正在整理研究上下文"
    elif focus:
        headline = f"正在推进「{_short(focus['task'], 30)}」"
    else:
        headline = "Agent 正在运行"

    outcome = (run or {}).get("outcome") or {}
    detail = STATE_DETAILS.get(state, "")
    if state == "blocked":
        detail = outcome.get("reason") or detail
        required = outcome.get("required_action")
        if required:
            detail += f" 需要处理：{required}"
    elif focus and state not in {"interrupted", "stopped", "suspended", "not_started"}:
        detail = focus.get("progress") or focus.get("criterion") or detail
    return {
        "state": state,
        "state_label": label,
        "tone": tone,
        "headline": headline,
        "detail": _short(detail, 200),
        "focus": focus,
        "active_jobs": [
            {
                "id": job,
                "short": job[:8],
                "status": row.get("status"),
                "status_label": job_meta(row.get("status"))["label"],
                "tone": job_meta(row.get("status"))["tone"],
                "observed_at": row.get("observed_at"),
            }
            for job, row in (run or {}).get("jobs", {}).items()
            if row.get("status") not in TERMINAL_JOB_STATES
        ],
        "waiting_jobs": (run or {}).get("pending", {}).get("jobs", []),
        "last_activity": (run or {}).get("last_activity"),
        "started_at": (run or {}).get("started_at"),
        "elapsed_seconds": (
            round(now - run["started_at"]) if run and run.get("started_at") else None
        ),
        "model": (run or {}).get("model"),
        "usage": (run or {}).get("usage", {}),
        "decisions": len((run or {}).get("decisions", [])),
        "outcome": {
            "status": outcome.get("status"),
            "reason": outcome.get("reason"),
        },
        "process_alive": any(
            session.get("alive") is True for session in (run or {}).get("sessions", [])
        ),
        "host_status": (run or {}).get("host_status"),
        "supervision_phase": (run or {}).get("supervision_phase"),
    }


def _jobs(run, plan):
    if run is None:
        return {"items": [], "counts": {"active": 0, "completed": 0, "failed": 0, "total": 0}}
    step_by_job = {}
    for step in plan.get("steps") or []:
        for job in step.get("jobs") or []:
            step_by_job[job["id"]] = step
    items = []
    for job, row in run.get("jobs", {}).items():
        status = row.get("status", "unobserved")
        meta = job_meta(status)
        step = step_by_job.get(job)
        items.append(
            {
                "id": job,
                "short": job[:8],
                "status": status,
                "status_label": meta["label"],
                "tone": meta["tone"],
                "observed_at": row.get("observed_at"),
                "submitted_at": row.get("submitted_at"),
                "name": row.get("name", ""),
                "command": row.get("command") or [],
                "source_files": row.get("source_files") or [],
                "gpu": bool(row.get("gpu")),
                "step": step.get("id") if step else None,
                "task": step.get("task") if step else "",
                "route": step.get("route") if step else "",
            }
        )
    items.sort(
        key=lambda item: (
            0 if item["status"] not in TERMINAL_JOB_STATES else 1,
            -(item.get("observed_at") or item.get("submitted_at") or 0),
        )
    )
    return {
        "items": items,
        "counts": {
            "active": sum(item["status"] not in TERMINAL_JOB_STATES for item in items),
            "completed": sum(item["status"] == "completed" for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
            "total": len(items),
        },
    }


def _schedule(project_root, now):
    directory = Path(project_root) / "runs/night-window"
    try:
        logs = sorted(directory.glob("launcher-*.log"))
    except OSError:
        logs = []
    if not logs:
        return None
    lines = tail_lines(logs[-1], max_bytes=32_000)
    for line in reversed(lines):
        marker = "Host paused until "
        if marker in line:
            value = line.split(marker, 1)[1].strip()
            try:
                starts = datetime.fromisoformat(value).timestamp()
            except ValueError:
                continue
            if starts <= now:
                continue
            return {
                "state": "waiting",
                "starts_at": starts,
                "text": "夜间研究窗口",
            }
    return None


def build_snapshot(project_root, selector="latest", *, now=None, process_observer=None):
    """Build the complete dashboard payload from files only."""
    now = _clock(now)
    root = Path(project_root).resolve()
    selected = select_run(root, selector)
    run_path = root / "trajectories" / selected if selected else None
    options = {}
    if process_observer is not None:
        options["process_observer"] = process_observer
    run = read_run(run_path, **options) if run_path else None
    jobs = run.get("jobs", {}) if run else {}
    plan = plan_payload(root / "plan/current.json", jobs)
    memory = knowledge.load_memory(root / "memory")
    skills = knowledge.load_skills(root / "skills")

    runs = []
    for identifier in run_ids(root)[:20]:
        path = root / "trajectories" / identifier
        state = run.get("state") if identifier == selected else None
        entry = summarize_run(path, selected_state=state)
        entry["selected"] = identifier == selected
        runs.append(entry)

    agent = _agent(run, plan, now)
    return {
        "generated_at": now,
        "project": {"name": "A3 研究台"},
        "selected_run": selected,
        "runs": runs,
        "agent": agent,
        "plan": plan,
        "memory": memory,
        "skills": skills,
        "jobs": _jobs(run, plan),
        "activity": _activity_events(run_path, run, plan) if run_path else [],
        "pulse": _pulse((run or {}).get("decisions", []), now, (run or {}).get("started_at")),
        "schedule": _schedule(root, now),
        "runtime": {
            "model": (run or {}).get("model"),
            "usage": (run or {}).get("usage", {}),
        },
    }
