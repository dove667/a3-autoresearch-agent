"""Run benchmark tasks through the real Host, real tools and real remote execution.

A task gets a thin project root: its own workspace and research plan, while the
project's memory, skills, prompts, embedding model and published source are shared
read-only. One real Host process runs per root, so tasks can run in parallel without
inventing a fake tool server.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from a3_research_agent.harness.plan.store import project_plan_file
from a3_research_agent.host.knowledge_service.lifecycle import KnowledgeService
from a3_research_agent.host.published_harness import published_source
from a3_research_agent.project import checkout_root

from . import status as benchmark_status
from .health import check as health_check

REPO = checkout_root() or Path(__file__).resolve().parents[3]
SHARED = {
    "A3_BENCHMARK_MEMORY_ROOT": REPO / "memory",
    "A3_BENCHMARK_SKILLS_ROOT": REPO / "skills",
    "A3_BENCHMARK_SKILLS_HISTORY_ROOT": REPO / ".runtime/skill-revisions",
    "A3_BENCHMARK_SKILLS_INDEX_ROOT": REPO / ".runtime/skill-index",
    "A3_BENCHMARK_READONLY_KNOWLEDGE": "1",
    "A3_BENCHMARK_PUBLISHED_HARNESS_ROOT": REPO / ".runtime/published-harness",
    "A3_EMBEDDING_MODEL": REPO / "model-cache/qwen3-embedding-0.6b/model.gguf",
}


@dataclass
class RealTask:
    id: str
    title: str
    question: str
    objective: str
    route: str
    task: str
    criterion: str
    instructions: str
    fixtures: tuple[tuple[str, str], ...]
    done: Callable[[Path, dict], bool]
    score: Callable[[Path, dict, Path], tuple[bool, str]]
    timeout: int = 900
    require_review: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    initial_status: str = "pending"
    initial_progress: str = ""


def _set_env(path: Path, values: dict[str, str]) -> None:
    from dotenv import set_key

    for key, value in values.items():
        set_key(str(path), key, str(value), quote_mode="never")


def _copy_fixture(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, symlinks=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _plan_for(task: RealTask) -> dict:
    return {
        "version": 2,
        "revision": 1,
        "direction": {
            "objective": task.objective,
            "metrics": [task.title],
            "gaps": [],
        },
        "routes": {
            task.route: {
                "question": task.question,
                "hypothesis": "真实 workspace 中的证据足以回答该问题",
                "experiment": "使用真实工具读取、执行或记录，不依赖外部提示",
                "decision_rule": "按 TASK.md 与完成判据核对证据",
                "status": "active",
                "priority": 0,
                "conclusion": "",
                "evidence": [],
            }
        },
        "steps": {
            task.id: {
                "route": task.route,
                "priority": 0,
                "task": task.task,
                "criterion": task.criterion,
                "status": task.initial_status,
                "depends_on": [],
                "jobs": [],
                "progress": task.initial_progress,
                "evidence": [],
            }
        },
        "reason": "benchmark task",
        "reviews": [],
        "reviewed_jobs": {},
    }


def materialize(task: RealTask, root: Path, model: str | None = None) -> Path:
    """Create only the parts that differ per task; knowledge stays shared."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "trajectories").mkdir()
    (root / ".runtime").mkdir()
    plan_file = project_plan_file(root.resolve())
    plan_file.parent.mkdir()
    (root / ".git").symlink_to(REPO / ".git")
    (root / "prompt").symlink_to(REPO / "prompt")
    (root / ".runtime/reference-sources").symlink_to(REPO / ".runtime/reference-sources")
    for source, destination in task.fixtures:
        _copy_fixture(REPO / source, root / "workspace" / destination)
    (root / "workspace/TASK.md").write_text(
        "工作区根是 read/list 的默认目录；下面的路径相对它，不要加 `workspace/` 前缀。\n\n"
        + task.instructions,
        encoding="utf-8",
    )
    plan_file.write_text(
        json.dumps(_plan_for(task), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    shutil.copy2(REPO / ".env", root / ".env")
    values = {name: str(path) for name, path in SHARED.items()}
    for key in ("A3_KNOWLEDGE_SERVICE_URL", "A3_KNOWLEDGE_SERVICE_TOKEN"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    if model:
        values.update(
            {
                "MODEL_NAME": model,
                "A3_UPSTREAM_RESPONSES_MODEL": model,
                "A3_WORKER_MODEL_NAME": model,
                "A3_SUMMARY_MODEL_NAME": model,
            }
        )
    _set_env(root / ".env", values)
    os.chmod(root / ".env", 0o600)
    return root


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_state(root: Path) -> tuple[Path | None, dict]:
    for state_path in (root / "trajectories").glob("*/.host/state.json"):
        state = _read_json(state_path)
        if state.get("endpoint") and isinstance(state.get("gateway"), dict):
            return state_path.parent.parent, state
    return None, {}


def _main_token(state: dict) -> str | None:
    gateway = state.get("gateway") or {}
    owner = gateway.get("main_owner")
    for token, value in (gateway.get("sessions") or {}).items():
        if value == owner:
            return token
    return next(iter(gateway.get("sessions") or {}), None)


def _start(root: Path, commit: str) -> subprocess.Popen:
    log = (root / "host.log").open("w")
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "a3_research_agent.cli.launch",
            "--project-root",
            str(root),
            "--harness-commit",
            commit,
        ],
        cwd=root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int, sig: int, *, group: bool = False) -> None:
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _signal_host(process: subprocess.Popen, sig: int) -> None:
    # The Host is a session leader. A group signal also stops the direct curl
    # child used for the model upstream, so a timed-out request cannot survive.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        process.send_signal(sig)


def _cleanup_processes(root: Path, run: Path | None) -> None:
    """Stop Host children even when the Host did not get a chance to shut down."""
    if run is not None:
        for record_path in run.glob("sessions/*/session.json"):
            record = _read_json(record_path)
            pid = (record.get("process") or {}).get("pid")
            if isinstance(pid, int) and _alive(pid):
                _kill_pid(pid, signal.SIGINT)
        pid_path = run / ".host/knowledge-service.pid"
        try:
            pid = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid and _alive(pid):
            _kill_pid(pid, signal.SIGINT, group=True)
    # The Host is its own session leader; the fallback covers a crashed launcher.
    for pid_path in root.glob("trajectories/*/.host/knowledge-service.pid"):
        try:
            pid = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            continue
        if _alive(pid):
            _kill_pid(pid, signal.SIGINT, group=True)


def _stop(endpoint: str, token: str, reason: str) -> str:
    try:
        response = httpx.post(
            endpoint + "/sessions/self/stop",
            headers={"Authorization": "Bearer " + token},
            json={"reason": reason},
            timeout=30,
            trust_env=False,
        )
        return f"stop {response.status_code}"
    except Exception as error:  # noqa: BLE001 - report the failed stop, keep evidence
        return f"stop failed: {type(error).__name__}"


def _trajectory(run: Path) -> list[dict]:
    path = run / "conversation/trajectory.jsonl"
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _calls(run: Path) -> list[dict]:
    return [row for row in _trajectory(run) if isinstance(row.get("tool"), str)]


def _retries(run: Path) -> int:
    return sum(1 for row in _trajectory(run) if row.get("model_retry"))


def _watchdog(process: subprocess.Popen) -> None:
    """Interrupt a Host even while its Agent is blocked in a model request."""
    if process.poll() is not None:
        return
    _signal_host(process, signal.SIGINT)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def _wait(root: Path, process: subprocess.Popen, task: RealTask, deadline: float) -> tuple[bool, bool, str]:
    done = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return done, False, "host exited"
        plan = _read_json(project_plan_file(root.resolve()))
        if task.done(root, plan):
            done = True
            # Give the Agent a short turn to record its review or stop itself.
            time.sleep(45)
            return done, True, "done"
        time.sleep(2)
    return done, False, "timeout"


def run_task(task: RealTask, output: Path, commit: str, model: str | None) -> dict:
    started = time.monotonic()
    root = (output / task.id).resolve()
    materialize(task, root, model)
    process = _start(root, commit)
    watchdog = threading.Timer(max(1, task.timeout), _watchdog, args=(process,))
    watchdog.daemon = True
    watchdog.start()
    run = state = {}
    timed_out = False
    error = ""
    try:
        deadline = time.monotonic() + task.timeout
        while time.monotonic() < deadline:
            run, state = _run_state(root)
            if run:
                break
            if process.poll() is not None:
                raise RuntimeError("Host exited before creating a run")
            time.sleep(2)
        else:
            raise RuntimeError("Host did not create a run in time")
        completed, reached, reason = _wait(root, process, task, deadline)
        if not reached:
            timed_out = reason == "timeout"
        token = _main_token(state)
        stop = "not needed"
        if process.poll() is None:
            if token:
                stop = _stop(state["endpoint"], token, "benchmark task complete")
            else:
                stop = "no main token"
        try:
            process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            # SIGINT lets launch_run run its finally block and suspend the Agent.
            _signal_host(process, signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
        _cleanup_processes(root, run or None)
        plan = _read_json(project_plan_file(root.resolve()))
        passed, detail = task.score(root, plan, run)
        calls = _calls(run)
        if task.require_review and not plan.get("reviews"):
            passed = False
            detail = f"{detail}; no plan review" if detail else "no plan review"
        return {
            "id": task.id,
            "title": task.title,
            "passed": passed,
            "detail": detail,
            "completed": completed,
            "timed_out": timed_out,
            "stop": stop,
            "seconds": round(time.monotonic() - started, 1),
            "run": str(run),
            "root": str(root),
            "plan_revision": plan.get("revision"),
            "plan_step": (plan.get("steps") or {}).get(task.id, {}).get("status"),
            "reviews": len(plan.get("reviews") or []),
            "calls": len(calls),
            "call_names": [call.get("tool") for call in calls],
            "model_retries": _retries(run),
        }
    except Exception as error_value:  # noqa: BLE001 - one broken task must not end the batch
        return {
            "id": task.id,
            "title": task.title,
            "passed": False,
            "detail": f"运行错误：{type(error_value).__name__}: {error_value}",
            "completed": False,
            "timed_out": timed_out,
            "stop": error,
            "seconds": round(time.monotonic() - started, 1),
            "run": str(run) if run else "",
            "root": str(root),
            "calls": len(_calls(run)) if run else 0,
            "call_names": [],
            "model_retries": _retries(run) if run else 0,
        }
    finally:
        watchdog.cancel()
        if process.poll() is None:
            _signal_host(process, signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
        _cleanup_processes(root, run or None)
        with (root / "host.log").open("a") as stream:
            stream.write("\n--- runner finished ---\n")


def report(results: list[dict], directory: Path, commit: str) -> dict:
    passed = sum(1 for row in results if row["passed"])
    summary = {
        "passed": passed,
        "total": len(results),
        "score": f"{passed}/{len(results)}",
        "tool_calls": sum(row.get("calls", 0) for row in results),
        "harness_commit": commit,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(
        json.dumps({"summary": summary, "tasks": results}, ensure_ascii=False, indent=2)
    )
    lines = [
        "# 真实 Harness Benchmark",
        "",
        f"- 通过 {summary['score']}；工具调用 {summary['tool_calls']} 次；Harness {commit[:12]}",
        "",
        "| 任务 | 结果 | 轮次/调用 | 说明 |",
        "|---|---|---|---|",
    ]
    for row in results:
        lines.append(
            f"| {row['title']} | {'通过' if row['passed'] else '失败'} | "
            f"{row.get('plan_revision', '-')}/{row.get('calls', 0)} | {row.get('detail', '')} |"
        )
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def run(
    only: list[str] | None = None,
    output: Path | None = None,
    jobs: int = 2,
    timeout: int | None = None,
    model: str | None = None,
    commit: str | None = None,
    attempts: int = 1,
    state: str = "open",
) -> dict:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
    preflight = health_check()
    if not preflight["healthy"]:
        raise SystemExit("Benchmark preflight failed: " + json.dumps(preflight, ensure_ascii=False))
    selected = benchmark_status.selected(TASKS, state, only)
    if not selected:
        raise SystemExit(f"No benchmark tasks in state {state!r}")
    if timeout:
        selected = [
            RealTask(**{**task.__dict__, "timeout": timeout}) for task in selected
        ]
    commit = commit or subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True
    ).strip()
    published_source(REPO, commit)
    directory = (output or Path("runs/checks") / f"real-benchmark-{time.strftime('%Y-%m-%d_%H-%M-%S')}").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # One local embedding service serves every task Host in this batch. Per-run
    # services would load the same model and index once per parallel task.
    for name, path in SHARED.items():
        os.environ.setdefault(name, str(path))
    shared_run = directory / "trajectories" / "shared-knowledge"
    shared_run.mkdir(parents=True, exist_ok=True)
    results_by_id = {}
    with KnowledgeService(REPO, shared_run, "benchmark") as knowledge:
        os.environ["A3_KNOWLEDGE_SERVICE_URL"] = knowledge.endpoint
        os.environ["A3_KNOWLEDGE_SERVICE_TOKEN"] = knowledge.token
        pending = list(selected)
        for attempt in range(max(1, attempts)):
            if not pending:
                break
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
                futures = {
                    pool.submit(run_task, task, directory, commit, model): task
                    for task in pending
                }
                for future in as_completed(futures):
                    row = future.result()
                    row["attempt"] = attempt + 1
                    results_by_id[row["id"]] = row
                    benchmark_status.record(
                        TASKS, [row], directory=directory, model=model
                    )
                    print(
                        f"{'PASS' if row['passed'] else 'FAIL'} {row['id']}: {row['detail']}",
                        flush=True,
                    )
                    report(list(results_by_id.values()), directory, commit)
            pending = [
                task
                for task in selected
                if not results_by_id.get(task.id, {}).get("passed", False)
            ]
            if pending and attempt + 1 < attempts:
                time.sleep(30)
    results = list(results_by_id.values())
    results.sort(key=lambda row: [task.id for task in TASKS].index(row["id"]))
    summary = report(results, directory, commit)
    summary["status"] = benchmark_status.summary(TASKS)
    summary["directory"] = str(directory)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def _json_file(root: Path, path: str) -> dict:
    return _read_json(root / "workspace" / path)


def _read_baseline_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-baseline.json")
    return (
        value.get("case") == "expert_development"
        and value.get("seeds") == [31000, 31001, 31002]
        and value.get("native_successes") == 3
    )


def _read_baseline_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-baseline.json")
    if (
        value.get("case") == "expert_development"
        and value.get("seeds") == [31000, 31001, 31002]
        and value.get("native_successes") == 3
    ):
        review = "review" if plan.get("reviews") else "no review"
        return True, f"真实 results.json 读取正确；{review}"
    return False, f"结果文件不符合：{value}"


TASKS = (
    RealTask(
        "read_baseline",
        "读取真实基线结果",
        "真实 ACT baseline 的 expert_development 用了哪些 seed，native_successes 是多少？",
        "完成 benchmark：读取真实基线结果并写出结构化结论",
        "bench-read",
        "读 experiments/2026-11-03-act-baseline/results/results.json 的 expert_development，写 results/bench-baseline.json，plan update done，然后 session_stop。",
        "结果文件 native_successes=3 且 seeds=[31000,31001,31002]。",
        "# Benchmark task\n\n"
        "读取 `experiments/2026-11-03-act-baseline/results/results.json`，确认 "
        "`cases.expert_development` 的 seeds 与 native_successes。\n\n"
        "把结果写入 `results/bench-baseline.json`，格式：\n"
        '`{"case":"expert_development","seeds":[31000,31001,31002],"native_successes":3}`\n\n'
        "完成后用 `plan(action=\"review\")` 记录结论、evidence 和 metrics，然后调用 "
        "`session_stop(reason=\"benchmark task complete\")`。\n",
        (
            ("workspace/experiments/2026-11-03-act-baseline/results/results.json",
             "experiments/2026-11-03-act-baseline/results/results.json"),
            ("workspace/experiments/2026-11-03-act-baseline/config.json",
             "experiments/2026-11-03-act-baseline/config.json"),
        ),
        _read_baseline_done,
        _read_baseline_score,
        timeout=900,
        tags=("read", "real-workspace"),
    ),
)



def _read_config_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-config.json")
    return value.get("seed_start") == 21000 and value.get("development_seeds") == [
        31000,
        31001,
        31002,
    ]


def _read_config_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-config.json")
    if value.get("seed_start") == 21000 and value.get("development_seeds") == [
        31000,
        31001,
        31002,
    ]:
        return True, "真实 config.json 读取正确"
    return False, f"结果文件不符合：{value}"


def _memory_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-memory.json")
    return value.get("id") == "d9ebbacf33e6" and "11/120" in str(value) and "7/120" in str(value)


def _memory_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-memory.json")
    if value.get("id") == "d9ebbacf33e6" and "11/120" in str(value) and "7/120" in str(value):
        return True, "真实 memory 检索正确"
    return False, f"结果文件不符合：{value}"


def _skill_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-skill.json")
    text = json.dumps(value, ensure_ascii=False)
    return value.get("skill") == "a3-mujoco-autoresearch" and "配对" in text and "闭环" in text


def _skill_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-skill.json")
    text = json.dumps(value, ensure_ascii=False)
    if value.get("skill") == "a3-mujoco-autoresearch" and "配对" in text and "闭环" in text:
        return True, "真实 skill 读取正确"
    return False, f"结果文件不符合：{value}"

TASKS += (
    RealTask(
        "read_config",
        "读取真实实验配置",
        "真实 ACT baseline 的数据 seed_start 和 development seeds 是多少？",
        "完成 benchmark：读取真实实验配置并写出结构化结论",
        "bench-config",
        "读 experiments/2026-11-03-act-baseline/config.json 的 seed_start 与 development seeds，写 results/bench-config.json，plan update done，然后 session_stop。",
        "结果文件 seed_start=21000 且 development_seeds=[31000,31001,31002]。",
        "# Benchmark task\n\n"
        "读取 `experiments/2026-11-03-act-baseline/config.json`，确认 `data.seed_start` "
        "与 `development_evaluation.seeds`。\n\n"
        "把结果写入 `results/bench-config.json`，格式：\n"
        '`{"seed_start":21000,"development_seeds":[31000,31001,31002]}`\n\n'
        "完成后调用 `session_stop(reason=\"benchmark task complete\")`。\n",
        (
            ("workspace/experiments/2026-11-03-act-baseline/config.json",
             "experiments/2026-11-03-act-baseline/config.json"),
        ),
        _read_config_done,
        _read_config_score,
        tags=("read", "real-workspace"),
    ),
    RealTask(
        "memory_pair_result",
        "检索真实跨轮记忆",
        "真实 memory 中 2×10 配对结果的 ID 和两个入盒数是什么？",
        "完成 benchmark：从共享 memory 检索真实配对结论",
        "bench-memory",
        "按 TASK.md 使用 memory 工具找到 2×10 配对结果，写出 results/bench-memory.json。",
        "结果文件 id=d9ebbacf33e6，且包含 11/120 与 7/120。",
        "# Benchmark task\n\n"
        "使用 `memory(action=\"search\", query=\"2×10 配对 入盒\")` 找到记录 2×10 "
        "配对结果的条目，再用 `memory(action=\"read\", id=...)` 核对内容。\n\n"
        "把结果写入 `results/bench-memory.json`，至少包含：\n"
        '`{"id":"d9ebbacf33e6","result":"2x10 5k 11/120, 20k 7/120"}`\n\n'
        "memory 是共享只读知识，不要写入或修改。完成后调用 "
        "`session_stop(reason=\"benchmark task complete\")`。\n",
        (),
        _memory_done,
        _memory_score,
        tags=("memory", "shared-knowledge"),
    ),
    RealTask(
        "skill_method",
        "读取真实方法技能",
        "真实技能库中 A3 策略实验的配对/闭环方法要点是什么？",
        "完成 benchmark：从共享 skills 读取真实方法规范",
        "bench-skill",
        "按 TASK.md 使用 skills 工具读取 a3-mujoco-autoresearch，写出 results/bench-skill.json。",
        "结果文件 skill=a3-mujoco-autoresearch，且包含配对与闭环。",
        "# Benchmark task\n\n"
        "使用 `skills(action=\"list\", query=\"A3 策略实验 配对 闭环\")` 找到方法技能，"
        "再 `skills(action=\"read\", id=\"a3-mujoco-autoresearch\")` 读取正文。\n\n"
        "把要点写入 `results/bench-skill.json`，至少包含：\n"
        '`{"skill":"a3-mujoco-autoresearch","points":["配对比较","闭环评测"]}`\n\n'
        "skills 是共享只读知识，不要修改。完成后调用 "
        "`session_stop(reason=\"benchmark task complete\")`。\n",
        (),
        _skill_done,
        _skill_score,
        tags=("skills", "shared-knowledge"),
    ),
)


def _cpu_job_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-job.json")
    ident = value.get("job_id")
    return (
        isinstance(ident, str)
        and len(ident) == 32
        and value.get("status") == "completed"
        and value.get("exit_code") == 0
    )


def _cpu_job_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-job.json")
    ident = value.get("job_id")
    if (
        isinstance(ident, str)
        and len(ident) == 32
        and value.get("status") == "completed"
        and value.get("exit_code") == 0
    ):
        return True, "真实 CPU 作业提交并完成"
    return False, f"结果文件不符合：{value}"


TASKS += (
    RealTask(
        "cpu_job",
        "提交并等待真实 CPU 作业",
        "通过真实 job_submit/job_wait 跑一个 CPU 作业，返回作业号和退出码。",
        "完成 benchmark：通过真实远端执行一次 CPU 作业",
        "bench-job",
        "按 TASK.md 提交并等待一个真实 CPU 作业，写出 results/bench-job.json。",
        "结果文件 job_id 为 32 位，status=completed，exit_code=0。",
        """# Benchmark task

使用 `job_submit(command=["python","-c","print('bench-ok')"])` 提交一个真实 CPU 作业（不要 gpu，不要 prepared_id）。用 `job_wait` 等到终态。

把回执写入 `results/bench-job.json`，格式：

`{"job_id":"<32位作业号>","status":"completed","exit_code":0}`

不要重复提交同一个作业。完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _cpu_job_done,
        _cpu_job_score,
        timeout=900,
        tags=("job", "remote"),
    ),
)



def _plan_add_done(root: Path, plan: dict) -> bool:
    return any(
        key != "plan_add_step"
        and isinstance(step, dict)
        and step.get("route") == "bench-plan"
        and "exit 0" in str(step.get("criterion", ""))
        for key, step in (plan.get("steps") or {}).items()
    )


def _plan_add_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    for key, step in (plan.get("steps") or {}).items():
        if key != "plan_add_step" and step.get("route") == "bench-plan" and "exit 0" in str(
            step.get("criterion", "")
        ):
            return True, f"plan 新增步骤 {key} 并写入判据"
    return False, "plan 没有新增带判据的步骤"


def _job_failure_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-failure.json")
    return (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and value.get("exit_code") == 1
        and any(word in str(value.get("diagnosis", "")).lower() for word in ("out of memory", "oom", "显存"))
    )


def _job_failure_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-failure.json")
    if (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and value.get("exit_code") == 1
        and any(word in str(value.get("diagnosis", "")).lower() for word in ("out of memory", "oom", "显存"))
    ):
        return True, "真实失败作业已诊断"
    return False, f"结果文件不符合：{value}"


def _batch_jobs_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-batch.json")
    jobs = value.get("jobs")
    return (
        isinstance(jobs, list)
        and len(jobs) == 2
        and all(
            isinstance(item, dict)
            and isinstance(item.get("job_id"), str)
            and len(item["job_id"]) == 32
            and item.get("exit_code") == 0
            for item in jobs
        )
    )


def _batch_jobs_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-batch.json")
    jobs = value.get("jobs")
    if (
        isinstance(jobs, list)
        and len(jobs) == 2
        and all(
            isinstance(item, dict)
            and isinstance(item.get("job_id"), str)
            and len(item["job_id"]) == 32
            and item.get("exit_code") == 0
            for item in jobs
        )
    ):
        return True, "两个真实 CPU 作业都完成"
    return False, f"结果文件不符合：{value}"


def _gpu_prepare_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-prepare.json")
    return str(value.get("prepared_id", "")).startswith("prepared-") and isinstance(
        value.get("preflight_job_id"), str
    )


def _gpu_prepare_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-prepare.json")
    if str(value.get("prepared_id", "")).startswith("prepared-") and isinstance(
        value.get("preflight_job_id"), str
    ):
        return True, "真实 GPU 请求已完成预检准备"
    return False, f"结果文件不符合：{value}"


TASKS += (
    RealTask(
        "plan_add_step",
        "在真实 plan 中新增实验步骤",
        "把下一个实验拆成可执行步骤写入 plan，包含明确判据。",
        "完成 benchmark：用真实 plan 工具拆解并新增步骤",
        "bench-plan",
        "按 TASK.md 在 bench-plan 下新增一个带 task 与 criterion 的 step，然后 session_stop。",
        "plan 中存在 bench-plan 下的新 step，criterion 含 exit 0。",
        """# Benchmark task

在 `bench-plan` route 下新增一个 step，id 用 `plan-added`，task 写“用 bench 脚本在 CPU 上检查环境”，criterion 写“命令 exit 0 且结果写入 results/bench-plan.txt”。

使用 `plan(action="update", revision=<当前revision>, steps=[...])`。完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _plan_add_done,
        _plan_add_score,
        timeout=900,
        tags=("plan", "harness"),
    ),
    RealTask(
        "job_failure",
        "诊断真实失败作业",
        "提交一个会失败的 CPU 作业，读取真实日志并说明失败原因。",
        "完成 benchmark：从真实失败作业诊断原因",
        "bench-failure",
        "按 TASK.md 提交失败 CPU 作业、等待终态、读取真实诊断，写出 results/bench-failure.json。",
        "结果文件 job_id 32 位、exit_code=1，diagnosis 含 out of memory/OOM/显存。",
        """# Benchmark task

用 `job_submit(command=["python","-c","import sys; print('CUDA out of memory', file=sys.stderr); sys.exit(1)"])` 提交一个真实 CPU 作业，等终态后用 `job_status`/真实日志确认失败原因。

不要猜测；把 `job_id`、`exit_code`、`diagnosis` 写入 `results/bench-failure.json`，例如：

`{"job_id":"<32位>","exit_code":1,"diagnosis":"CUDA out of memory"}`

完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _job_failure_done,
        _job_failure_score,
        timeout=900,
        tags=("job", "failure"),
    ),
    RealTask(
        "batch_jobs",
        "并行提交两个真实 CPU 作业",
        "把两个互不依赖的 CPU 检查作业提交出去并都等到完成。",
        "完成 benchmark：用 job 工具并行推进两个独立作业",
        "bench-batch",
        "按 TASK.md 提交两个独立 CPU 作业并等待，写出 results/bench-batch.json。",
        "结果文件 jobs 恰好两条，两个 job_id 均为 32 位且 exit_code=0。",
        """# Benchmark task

两个互不依赖的 CPU 作业都要提交：命令分别是 `["python","-c","print('bench-a')"]` 和 `["python","-c","print('bench-b')"]`。可以用一次 `job_submit(jobs=[...])`，也可以连续两次 `job_submit`；两种都算有效。

用 `job_wait` 等到两个都终态，把两条回执写入 `results/bench-batch.json`：

`{"jobs":[{"job_id":"<32位>","exit_code":0},{"job_id":"<32位>","exit_code":0}]}`

完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _batch_jobs_done,
        _batch_jobs_score,
        timeout=900,
        tags=("job", "batch", "remote"),
    ),
    RealTask(
        "gpu_prepare",
        "准备真实 GPU 请求并跑 CPU 预检",
        "把 GPU 请求保存下来并在远端 CPU 完成真实预检，不启动 GPU 训练。",
        "完成 benchmark：真实 GPU 请求准备与预检",
        "bench-prepare",
        "按 TASK.md 用 job_prepare 准备 GPU 请求，等待 preflight_job_id，写出 results/bench-prepare.json。",
        "结果文件 prepared_id 以 prepared- 开头，且包含 preflight_job_id。",
        """# Benchmark task

用 `job_prepare(command=["python","bench/preflight.py"], attachments=[{"path":"bench/preflight.py"}])` 准备一个 GPU 请求；`job_prepare` 会在远端 CPU 跑真实预检。等待返回的 `preflight_job_id` 完成后，不要提交 GPU 训练。

把 `prepared_id` 与 `preflight_job_id` 写入 `results/bench-prepare.json`：

`{"prepared_id":"prepared-...","preflight_job_id":"<32位>"}`

完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (("src/a3_research_agent/benchmark/fixtures/preflight.py", "bench/preflight.py"),),
        _gpu_prepare_done,
        _gpu_prepare_score,
        timeout=900,
        tags=("job", "gpu-prepare", "remote"),
    ),
)



def _review_done(root: Path, plan: dict) -> bool:
    return any(review.get("route") == "bench-review" for review in (plan.get("reviews") or []))


def _review_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    for review in plan.get("reviews") or []:
        if review.get("route") == "bench-review" and any(
            "3" in str(item) for item in review.get("metrics") or []
        ):
            return True, "真实结果已写入 plan review 并带 metrics"
    return False, "plan review 缺失或没有实测 metrics"


def _blocked_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-blocked.json")
    return value.get("status") == "blocked" and bool(value.get("reason"))


def _blocked_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-blocked.json")
    called = any(call.get("tool") == "research_status" for call in _calls(run))
    if value.get("status") == "blocked" and value.get("reason") and called:
        return True, "权限阻塞已通过 research_status 上报并留证"
    return False, f"上报不完整：target={value}, research_status={called}"


def _recover_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-recover.json")
    step = (plan.get("steps") or {}).get("recover_submit") or {}
    return (
        isinstance(value.get("job_id"), str)
        and bool(value.get("checked"))
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    )


def _recover_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-recover.json")
    calls = _calls(run)
    submits = [call for call in calls if call.get("tool") == "job_submit"]
    checked = any(call.get("tool") in {"job_list", "job_status"} for call in calls)
    step = (plan.get("steps") or {}).get("recover_submit") or {}
    if (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and bool(value.get("checked"))
        and len(submits) == 1
        and checked
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    ):
        return True, "只提交一次，查询后写结果并用 plan evidence 关闭步骤"
    return False, (
        f"提交/核对/闭合不完整：submits={len(submits)}, checked={checked}, "
        f"step={step.get('status')}, target={value}"
    )


TASKS += (
    RealTask(
        "review_result",
        "用真实结果写 plan review",
        "读真实 baseline 结果，把实测数字写成 plan review 并决定路线。",
        "完成 benchmark：从真实结果形成带 metrics 的复盘",
        "bench-review",
        "读 experiments/2026-11-03-act-baseline/results/results.json，用 plan review 写 native_successes=3，然后 session_stop。",
        "bench-review 的 review 含实测 metrics（native_successes=3）。",
        """# Benchmark task

读取 `experiments/2026-11-03-act-baseline/results/results.json`，确认 `cases.expert_development.native_successes`。

然后调用 `plan(action="review", revision=<当前revision>, id="bench-review", decision="continue", conclusion="expert_development native_successes=3", evidence=["experiments/2026-11-03-act-baseline/results/results.json"], metrics=["expert_development native_successes=3"])`。

完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (
            ("workspace/experiments/2026-11-03-act-baseline/results/results.json", "experiments/2026-11-03-act-baseline/results/results.json"),
            ("workspace/experiments/2026-11-03-act-baseline/config.json", "experiments/2026-11-03-act-baseline/config.json"),
        ),
        _review_done,
        _review_score,
        timeout=900,
        require_review=True,
        tags=("plan", "review"),
    ),
    RealTask(
        "blocked_report",
        "缺权限时如实上报",
        "当前路线需要 8 卡权限，不能自行绕过；把阻塞状态上报操作者。",
        "完成 benchmark：权限不足时停止并上报",
        "bench-blocked",
        "按 TASK.md 调用 research_status 上报 blocked，写出 results/bench-blocked.json，然后 session_stop。",
        "结果文件 status=blocked、reason 非空；trajectory 有 research_status 调用。",
        """# Benchmark task

当前 `bench-blocked` 步骤需要操作者开通 8 卡权限。不要尝试提交 GPU 作业或绕过权限。

调用 `research_status(status="blocked", reason="需要操作者开通 8 卡权限", kind="permission", required_action="opens 8-GPU permission")`，然后把状态与原因写入 `results/bench-blocked.json`：

`{"status":"blocked","reason":"需要操作者开通 8 卡权限"}`

完成后调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _blocked_done,
        _blocked_score,
        timeout=600,
        initial_status="blocked",
        initial_progress="需要操作者开通 8 卡权限",
        tags=("blocked", "status"),
    ),
    RealTask(
        "recover_submit",
        "提交不确定时先查再决定",
        "提交一个后台 CPU 作业后模拟回执不确定；先查作业，确认前不要重复提交。",
        "完成 benchmark：恢复不确定提交而不重复",
        "bench-recover",
        "按 TASK.md 只提交一个 sleep 作业，用 job_list/job_status 核对，写出 results/bench-recover.json 并用 plan evidence 关闭步骤。",
        "结果文件 job_id 32 位、checked=true；trajectory 恰好一次 job_submit 且至少一次 job_list/job_status；plan step 为 done 且有 evidence。",
        """# Benchmark task

先提交一个真实 CPU 作业：`job_submit(command=["python","-c","import time; time.sleep(30)"])`。

然后假设提交回执可能丢失：先用 `job_list` 或 `job_status` 核对这个作业是否已经建起来，确认之前不要重复提交。确认后把结果写入 `results/bench-recover.json`：

`{"job_id":"<32位>","status":"running 或 completed","checked":true}`

完成后用 `plan(action="update", revision=<当前revision>, steps=[{"id":"recover_submit","status":"done","evidence":["results/bench-recover.json"]}])` 关闭步骤，再调用 `session_stop(reason="benchmark task complete")`。
""",
        (),
        _recover_done,
        _recover_score,
        timeout=900,
        tags=("job", "recovery"),
    ),
)



def _parallel_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-parallel.json")
    jobs = value.get("jobs")
    step = (plan.get("steps") or {}).get("parallel_batch4") or {}
    return (
        isinstance(jobs, list)
        and len(jobs) == 4
        and len({item.get("job_id") for item in jobs if isinstance(item, dict)}) == 4
        and all(item.get("exit_code") == 0 for item in jobs if isinstance(item, dict))
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    )


def _parallel_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-parallel.json")
    jobs = value.get("jobs")
    step = (plan.get("steps") or {}).get("parallel_batch4") or {}
    if (
        isinstance(jobs, list)
        and len(jobs) == 4
        and len({item.get("job_id") for item in jobs if isinstance(item, dict)}) == 4
        and all(item.get("exit_code") == 0 for item in jobs if isinstance(item, dict))
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    ):
        return True, "4 个真实 CPU 作业并排完成"
    return False, f"并行 job 不完整：target={value}, step={step.get('status')}"


def _export_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-export.json")
    step = (plan.get("steps") or {}).get("export_reuse") or {}
    return (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and str(value.get("export_id", "")).startswith("export-")
        and "probe.txt" in (value.get("files") or [])
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    )


def _export_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-export.json")
    step = (plan.get("steps") or {}).get("export_reuse") or {}
    names = {call.get("tool") for call in _calls(run)}
    needed = {"job_submit", "job_wait", "job_artifact", "export_create", "export_status", "export_manifest"}
    if (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and str(value.get("export_id", "")).startswith("export-")
        and "probe.txt" in (value.get("files") or [])
        and step.get("status") == "done"
        and bool(step.get("evidence"))
        and needed <= names
    ):
        return True, "真实作业产物已读取、封存并核对 manifest"
    return False, f"export 不完整：target={value}, missing={sorted(needed - names)}"


def _gpu_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-gpu.json")
    step = (plan.get("steps") or {}).get("gpu_submit") or {}
    return (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and str(value.get("prepared_id", "")).startswith("prepared-")
        and value.get("exit_code") == 0
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    )


def _gpu_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-gpu.json")
    step = (plan.get("steps") or {}).get("gpu_submit") or {}
    names = {call.get("tool") for call in _calls(run)}
    if (
        isinstance(value.get("job_id"), str)
        and len(value["job_id"]) == 32
        and str(value.get("prepared_id", "")).startswith("prepared-")
        and value.get("exit_code") == 0
        and step.get("status") == "done"
        and bool(step.get("evidence"))
        and {"job_prepare", "job_submit", "job_wait"} <= names
    ):
        return True, "真实 GPU 请求完成预检、提交并成功退出"
    return False, f"GPU 提交不完整：target={value}"


def _loop_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-followup.json")
    step = (plan.get("steps") or {}).get("follow-up") or {}
    review = any(item.get("route") == "bench-loop" for item in (plan.get("reviews") or []))
    return (
        isinstance(value.get("job_id"), str)
        and value.get("exit_code") == 0
        and step.get("status") == "done"
        and bool(step.get("evidence"))
        and review
    )


def _loop_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-followup.json")
    step = (plan.get("steps") or {}).get("follow-up") or {}
    reviews = [item for item in (plan.get("reviews") or []) if item.get("route") == "bench-loop"]
    names = {call.get("tool") for call in _calls(run)}
    if (
        isinstance(value.get("job_id"), str)
        and value.get("exit_code") == 0
        and step.get("status") == "done"
        and bool(step.get("evidence"))
        and reviews
        and any("3" in str(metric) for metric in reviews[-1].get("metrics") or [])
        and {"job_submit", "job_wait", "plan"} <= names
    ):
        return True, "读取结果、提交作业、复盘并关闭步骤的完整闭环"
    return False, f"计划闭环不完整：step={step.get('status')}, reviews={len(reviews)}"


def _memory_guard_done(root: Path, plan: dict) -> bool:
    value = _json_file(root, "results/bench-memory-decision.json")
    step = (plan.get("steps") or {}).get("memory_guard") or {}
    return (
        value.get("decision") == "do_not_rerun"
        and value.get("memory_id") == "d9ebbacf33e6"
        and "11/120" in str(value)
        and "7/120" in str(value)
        and step.get("status") == "done"
        and bool(step.get("evidence"))
    )


def _memory_guard_score(root: Path, plan: dict, run: Path) -> tuple[bool, str]:
    value = _json_file(root, "results/bench-memory-decision.json")
    step = (plan.get("steps") or {}).get("memory_guard") or {}
    names = {call.get("tool") for call in _calls(run)}
    if (
        value.get("decision") == "do_not_rerun"
        and value.get("memory_id") == "d9ebbacf33e6"
        and "11/120" in str(value)
        and "7/120" in str(value)
        and step.get("status") == "done"
        and bool(step.get("evidence"))
        and "job_submit" not in names
    ):
        return True, "memory 证据支持不重复实验，且没有提交作业"
    return False, f"memory 防重复不完整：target={value}, job_submit={'job_submit' in names}"


TASKS += (
    RealTask(
        "parallel_batch4",
        "并行推进四个真实作业",
        "把四个互不依赖的 CPU 检查作业一次或多次提交，并全部等到完成。",
        "完成 benchmark：job controller 并行推进四个独立作业",
        "bench-parallel",
        "按 TASK.md 并行提交 4 个独立 CPU 作业，等全部完成，写 results/bench-parallel.json，plan done，然后 session_stop。",
        "结果文件 jobs 恰好四条，四个不同 job_id 均 exit_code=0；plan step done 且有 evidence。",
        """# Benchmark task

四个互不依赖的 CPU 作业都要提交：命令分别是 `["python","-c","print('bench-0')"]`、`["python","-c","print('bench-1')"]`、`["python","-c","print('bench-2')"]`、`["python","-c","print('bench-3')"]`。

可以用一次 `job_submit(jobs=[...])`，也可以多次提交；用 `job_wait` 等到四个都终态。把四条回执写入 `results/bench-parallel.json`：

`{"jobs":[{"job_id":"<32位>","exit_code":0}, ... 共四条]}`

完成后用 plan update 把 `parallel_batch4` 标为 done、evidence 指向结果文件，再 session_stop。
""",
        (),
        _parallel_done,
        _parallel_score,
        timeout=900,
        tags=("job", "parallel", "remote"),
    ),
    RealTask(
        "export_reuse",
        "封存并复用真实作业产物",
        "提交一个写文件的 CPU 作业，读取产物，封存为 export 并核对 manifest。",
        "完成 benchmark：真实 job artifact 与 export 复用链路",
        "bench-export",
        "按 TASK.md 提交写文件作业，读 artifact、export_create/status/manifest，写 results/bench-export.json，plan done，然后 session_stop。",
        "结果文件 job_id 32 位、export_id 以 export- 开头、files 含 probe.txt；相关工具都调用；plan step done。",
        """# Benchmark task

1. `job_submit(command=["python","bench/probe_output.py"], attachments=[{"path":"bench/probe_output.py"}])`，等终态。
2. `job_artifact(job_id=..., path="probe.txt", area="outputs")` 读取产物。
3. `export_create(job_id=..., path="probe.txt", area="outputs")`，用 `export_status` 等到 ready，再 `export_manifest`。
4. 把 `job_id`、`export_id`、manifest 中的文件列表写入 `results/bench-export.json`，例如：

`{"job_id":"<32位>","export_id":"export-...","files":["probe.txt"]}`

完成后 plan update 把 `export_reuse` 标为 done、evidence 指向结果文件，再 session_stop。
""",
        (("src/a3_research_agent/benchmark/fixtures/probe_output.py", "bench/probe_output.py"),),
        _export_done,
        _export_score,
        timeout=900,
        tags=("job", "export", "tools"),
    ),
    RealTask(
        "gpu_submit",
        "真实 GPU 请求预检后提交",
        "准备一个真实 GPU 请求，等 CPU 预检通过，正式提交并等待 GPU 作业完成。",
        "完成 benchmark：GPU 预检、提交与真实执行",
        "bench-gpu",
        "按 TASK.md 用 job_prepare 准备 GPU 请求，等 preflight，job_submit(prepared_id)，等作业完成并写 results/bench-gpu.json。",
        "结果文件 job_id 32 位、prepared_id 以 prepared- 开头、exit_code=0；plan step done。",
        """# Benchmark task

用 `job_prepare(command=["python","bench/gpu_probe.py"], attachments=[{"path":"bench/gpu_probe.py"}])` 准备真实 GPU 请求。等返回的 `preflight_job_id` 完成后，用 `job_submit(prepared_id=...)` 正式提交，等 GPU 作业终态。

把 `prepared_id`、`job_id`、`exit_code` 写入 `results/bench-gpu.json`：

`{"prepared_id":"prepared-...","job_id":"<32位>","exit_code":0}`

完成后 plan update 把 `gpu_submit` 标为 done、evidence 指向结果文件，再 session_stop。
""",
        (("src/a3_research_agent/benchmark/fixtures/gpu_probe.py", "bench/gpu_probe.py"),),
        _gpu_done,
        _gpu_score,
        timeout=1200,
        tags=("job", "gpu", "remote"),
    ),
    RealTask(
        "plan_loop",
        "连续完成一次实验闭环",
        "读取真实结果，拆出新步骤，提交并等待作业，写结果，复盘并关闭步骤。",
        "完成 benchmark：plan→执行→review 的连续闭环",
        "bench-loop",
        "按 TASK.md 读取真实结果、新增 follow-up 步骤、提交 CPU 作业、写结果、plan review 并关闭步骤。",
        "follow-up step done 且有 evidence；bench-loop review metrics 含 3；真实作业 exit_code=0。",
        """# Benchmark task

1. 读取 `experiments/2026-11-03-act-baseline/results/results.json`，确认 `cases.expert_development.native_successes=3`。
2. 用 plan update 在 `bench-loop` 下新增 step `follow-up`，task 写“CPU follow-up”，criterion 写“job exit 0 且结果写入 results/bench-followup.json”。
3. 提交真实 CPU 作业 `["python","-c","print('follow-up')"]`，用 `job_wait` 完成。
4. 把 job_id 和 exit_code 写入 `results/bench-followup.json`。
5. plan update 把 `follow-up` 标为 done，evidence 指向该结果文件。
6. plan review `bench-loop`，decision continue，metrics 含 `native_successes=3`，evidence 指向 results.json。
7. session_stop。
""",
        (("workspace/experiments/2026-11-03-act-baseline/results/results.json", "experiments/2026-11-03-act-baseline/results/results.json"),),
        _loop_done,
        _loop_score,
        timeout=1200,
        require_review=True,
        tags=("plan", "job", "review"),
    ),
    RealTask(
        "memory_guard",
        "用长期记忆避免重复实验",
        "先查真实 memory 的 2×10 配对结论，再决定是否有必要重跑。",
        "完成 benchmark：memory 证据阻止低效重复",
        "bench-memory-guard",
        "按 TASK.md 用 memory 检索 2×10 配对结论，写 results/bench-memory-decision.json，禁止 job_submit，plan done，然后 session_stop。",
        "decision=do_not_rerun、memory_id=d9ebbacf33e6，包含 11/120 与 7/120；没有 job_submit；plan step done。",
        """# Benchmark task

使用 `memory` 工具检索并读取 2×10 配对结果（5k 11/120、20k 7/120，memory id `d9ebbacf33e6`）。根据该结论判断没有必要现在重跑，不要调用 `job_submit`。

把决定写入 `results/bench-memory-decision.json`：

`{"decision":"do_not_rerun","memory_id":"d9ebbacf33e6","reason":"2x10 5k=11/120, 20k=7/120，重复运行不产生新信息"}`

完成后 plan update 把 `memory_guard` 标为 done、evidence 指向结果文件，再 session_stop。
""",
        (),
        _memory_guard_done,
        _memory_guard_score,
        timeout=600,
        tags=("memory", "plan"),
    ),
)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated task ids")
    parser.add_argument("--out", type=Path, help="Output directory")
    parser.add_argument("--jobs", type=int, default=2, help="Parallel real Hosts")
    parser.add_argument("--timeout", type=int, help="Per-task wall seconds")
    parser.add_argument("--model", help="Override the deployment model")
    parser.add_argument("--commit", help="Published Harness commit (default HEAD)")
    parser.add_argument("--attempts", type=int, default=1, help="Retry failed tasks this many times")
    parser.add_argument(
        "--state",
        choices=("open", "completed", "stable", "all"),
        default="open",
        help="Select tasks by persisted state (ignored with --only)",
    )
    args = parser.parse_args()
    run(
        only=[item for item in (args.only or "").split(",") if item],
        output=args.out,
        jobs=args.jobs,
        timeout=args.timeout,
        model=args.model,
        commit=args.commit,
        attempts=args.attempts,
        state=args.state,
    )


if __name__ == "__main__":
    main()

