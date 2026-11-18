"""The local dashboard observes live run, plan, memory and skill state."""

import json
import socket
import time
import urllib.request

from fastapi.testclient import TestClient

from a3_research_agent.harness import tool_catalog
from a3_research_agent.host.dashboard.lifecycle import DashboardService
from a3_research_agent.host.dashboard.server import create_app
from a3_research_agent.host.dashboard.snapshot import build_snapshot

JOB_RUNNING = "a" * 32
JOB_DONE = "b" * 32
RUN = "2026-11-17_10-00-00-000000"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def build_project(root, *, alive=True):
    run = root / "trajectories" / RUN
    write(
        run / "startup.json",
        {
            "model": "test-model",
            "input_manifest": {"time": "2026-11-17T10:00:00+08:00"},
            "context_budget": {"main_context_tokens": 1000},
        },
    )
    write(run / ".host/state.json", {"status": "running", "operator_token": "secret"})
    write(
        run / "supervision.json",
        {
            "phase": "supervising",
            "sessions": [{"session_id": "session-1", "status": "running"}],
        },
    )
    write(run / "conversation/run-outcome.json", {"status": "continue", "reason": "继续"})
    write(
        run / "conversation/operational-state.json",
        {
            "version": 2,
            "revision": 4,
            "reads": {},
            "jobs": {
                JOB_RUNNING: {"status": "queued", "observed_at": 1_800_000_100},
                JOB_DONE: {"status": "completed", "observed_at": 1_800_000_200},
            },
            "counters": {},
        },
    )
    write(
        run / "conversation/pending-calls.json",
        {
            "output": [
                {
                    "type": "function_call",
                    "name": "job_wait",
                    "arguments": json.dumps({"job_ids": [JOB_RUNNING]}),
                }
            ],
            "results": {},
        },
    )
    (run / "job-inputs.jsonl").write_text(
        json.dumps(
            {
                "job_id": JOB_RUNNING,
                "experiment_id": "session-1",
                "name": "预算探针",
                "command": ["python", "scripts/eval.py", "--seed", "10010"],
                "gpu": True,
                "source_files": [
                    {"path": "scripts/eval.py", "sha256": "hash", "bytes": 10}
                ],
                "submitted_at": 1_800_000_090,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "model-usage.jsonl").write_text(
        json.dumps({"estimated_input_tokens": 400, "usage": {"input_tokens": 400}}) + "\n",
        encoding="utf-8",
    )
    (run / "conversation/trajectory.jsonl").write_text(
        json.dumps({"response": {"created_at": 1_800_000_150}}) + "\n",
        encoding="utf-8",
    )
    write(
        run / "sessions/session-1/session.json",
        {
            "session_id": "session-1",
            "closed": False,
            "process": {"pid": 1234, "started": "fake"},
        },
    )
    write(
        root / "plan/current.json",
        {
            "version": 2,
            "revision": 3,
            "reason": "",
            "direction": {
                "objective": "提高入盒准确率",
                "metrics": ["报告 2×5 与 2×10"],
                "gaps": [],
            },
            "routes": {
                "r1": {
                    "question": "扩采能否提升 2×10？",
                    "hypothesis": "更多数据提高吞吐",
                    "experiment": "同种子配对评测",
                    "decision_rule": "2×10 提升且 2×5 不降",
                    "status": "active",
                    "priority": 10,
                    "conclusion": "",
                    "evidence": [],
                }
            },
            "steps": {
                "s1": {
                    "task": "分析失败回合",
                    "criterion": "写出失败分类",
                    "status": "active",
                    "route": "r1",
                    "priority": 5,
                    "depends_on": [],
                    "jobs": [],
                    "progress": "",
                    "evidence": [],
                },
                "s2": {
                    "task": "等待配对评测",
                    "criterion": "拿到逐回合结果",
                    "status": "active",
                    "route": "r1",
                    "priority": 4,
                    "depends_on": [],
                    "jobs": [JOB_RUNNING],
                    "progress": "评测已提交",
                    "evidence": [],
                },
            },
            "reviews": [],
            "reviewed_jobs": {},
        },
    )
    memory = root / "memory/entries.jsonl"
    memory.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "id": "m1",
            "kind": "lesson",
            "text": "旧的失败经验",
            "scope": "2×10",
            "refs": ["jobs/old"],
            "tags": ["旧"],
            "ts": 1_800_000_000,
            "status": "active",
            "supersedes": "",
        },
        {
            "id": "m2",
            "kind": "note",
            "text": "已经归档的记录",
            "ts": 1_800_000_001,
            "status": "archived",
        },
        {
            "id": "m3",
            "kind": "lesson",
            "text": "新的失败经验",
            "scope": "2×10",
            "refs": ["jobs/new"],
            "tags": ["新"],
            "ts": 1_800_000_002,
            "status": "active",
            "supersedes": "m1",
        },
    ]
    memory.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    skill = root / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\nmetadata:\n  version: 0.1.0\n---\n\n# 方法\n\n正文内容。\n",
        encoding="utf-8",
    )
    write(
        skill / "assets/catalog-entry.json",
        {
            "id": "demo",
            "version": "0.1.0",
            "status": "active",
            "description": "一个演示技能",
            "keywords": ["演示"],
            "local_source": "skills/demo/SKILL.md",
        },
    )
    (root / "trajectories/latest-run.txt").write_text(str(run) + "\n", encoding="utf-8")
    return run, (lambda pid, started: alive)


def test_overview_preserves_full_task_progress_and_next_hint(tmp_path):
    run, observer = build_project(tmp_path)
    (run / "conversation/pending-calls.json").unlink()
    plan_path = tmp_path / "plan/current.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    task = "按种子对比全量评测的入盒准确率和通过率，" * 4 + "汇总失败类型。"
    progress = "对照组已完成，正在核对每个回合的评测记录和指标口径。\n" * 9
    plan["steps"]["s1"].update(task=task, progress=progress)
    write(plan_path, plan)

    snapshot = build_snapshot(tmp_path, "latest", process_observer=observer)

    assert snapshot["agent"]["headline"] == f"正在推进「{task}」"
    assert snapshot["agent"]["detail"] == progress
    assert snapshot["plan"]["next_hint"] == f"下一步：{task}"


def test_snapshot_reads_live_run_plan_memory_and_skills(tmp_path):
    _run, observer = build_project(tmp_path)
    snapshot = build_snapshot(tmp_path, "latest", now=1_800_000_300, process_observer=observer)
    assert snapshot["selected_run"] == RUN
    assert snapshot["agent"]["state"] == "waiting_jobs"
    assert snapshot["agent"]["usage"]["percent"] == 40
    assert snapshot["plan"]["progress"] == {"done": 0, "total": 2, "percent": 0}
    assert snapshot["plan"]["focus"]["id"] == "s1"
    assert snapshot["plan"]["routes"][0]["id"] == "r1"
    assert snapshot["memory"]["counts"] == {
        "effective": 1,
        "superseded": 1,
        "archived": 1,
        "total": 3,
    }
    assert snapshot["skills"]["counts"]["active"] == 1
    assert snapshot["skills"]["items"][0]["body"].startswith("# 方法")
    assert snapshot["tools"]["counts"]["total"] == len(tool_catalog.TOOLS)
    assert snapshot["tools"]["families"][0]["label"] == "文件与工作区"
    plan_tool = next(item for item in snapshot["tools"]["items"] if item["name"] == "plan")
    assert plan_tool["family"] == "knowledge"
    assert any(
        parameter["name"] == "action" and parameter["required"]
        for parameter in plan_tool["parameters"]
    )
    web_tool = next(item for item in snapshot["tools"]["items"] if item["name"] == "web_search")
    assert web_tool["optional"] is True
    assert snapshot["jobs"]["counts"]["active"] == 1
    running = next(item for item in snapshot["jobs"]["items"] if item["id"] == JOB_RUNNING)
    assert running["name"] == "预算探针"
    assert running["command"] == ["python", "scripts/eval.py", "--seed", "10010"]
    assert running["source_files"] == ["scripts/eval.py"]
    assert running["gpu"] is True
    assert running["submitted_at"] == 1_800_000_090
    assert running["task"] == "等待配对评测"
    assert "secret" not in json.dumps(snapshot)

    # A later request sees changed source files; no dashboard-side cache is involved.
    plan = json.loads((tmp_path / "plan/current.json").read_text(encoding="utf-8"))
    plan["steps"]["s1"]["task"] = "新的分析任务"
    write(tmp_path / "plan/current.json", plan)
    (tmp_path / "memory/entries.jsonl").write_text(
        json.dumps(
            {
                "id": "m4",
                "kind": "result",
                "text": "刚写入的新结果",
                "ts": 1_800_000_310,
                "status": "active",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    refreshed = build_snapshot(tmp_path, "latest", now=1_800_000_320, process_observer=observer)
    assert refreshed["plan"]["steps"][0]["task"] == "新的分析任务"
    assert refreshed["memory"]["items"][0]["text"] == "刚写入的新结果"
    assert refreshed["selected_run"] == RUN


def test_interrupted_and_empty_states_are_explicit(tmp_path):
    build_project(tmp_path, alive=False)
    snapshot = build_snapshot(
        tmp_path, "latest", now=1_800_000_300, process_observer=lambda pid, started: False
    )
    assert snapshot["agent"]["state"] == "interrupted"
    assert snapshot["agent"]["headline"] == "研究进程已中断"

    empty = tmp_path / "empty"
    empty.mkdir()
    snapshot = build_snapshot(empty, "latest", now=time.time())
    assert snapshot["selected_run"] is None
    assert snapshot["agent"]["state"] == "not_started"
    assert snapshot["plan"]["progress"]["total"] == 0
    assert snapshot["memory"]["counts"]["total"] == 0


def test_api_serves_snapshot_and_static_assets(tmp_path):
    build_project(tmp_path)
    app = create_app(tmp_path, process_observer=lambda pid, started: True)
    client = TestClient(app)
    response = client.get("/api/snapshot")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["agent"]["state"] == "waiting_jobs"
    page = client.get("/")
    assert page.status_code == 200
    assert "Tools &amp; Skills" in page.text
    assert "page-reviews" not in page.text
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/snapshot?run=missing").status_code == 404


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_dashboard_lifecycle_starts_and_stops_with_its_owner(tmp_path):
    build_project(tmp_path)
    port = free_port()
    service = DashboardService(tmp_path, port=port)
    assert service.start() is True
    with urllib.request.urlopen(service.url + "/api/health", timeout=3) as response:
        assert json.loads(response.read()) == {"ok": True, "dashboard": "a3-research"}
    service.stop()
    assert service.thread is not None and not service.thread.is_alive()


def test_dashboard_lifecycle_refuses_an_occupied_port(tmp_path):
    build_project(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        service = DashboardService(tmp_path, port=occupied.getsockname()[1])
        assert service.start() is False
        service.stop()


def test_overview_reports_tokens_and_spend(tmp_path):
    """The overview shows what the run cost, priced from its own usage journal."""
    from a3_research_agent.host.dashboard import runs

    journal = tmp_path / "model-usage.jsonl"
    journal.write_text(
        json.dumps(
            {
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 200_000,
                    "input_tokens_details": {"cached_tokens": 400_000},
                },
                "cost": 12.5,
                "cost_currency": "cny",
            }
        )
        + "\n"
    )
    usage = runs._usage(tmp_path, {})
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 1_000_000
    assert usage["cached_tokens"] == 400_000
    assert usage["output_tokens"] == 200_000
    assert usage["cost"] == 12.5
    assert usage["currency"] == "cny"
