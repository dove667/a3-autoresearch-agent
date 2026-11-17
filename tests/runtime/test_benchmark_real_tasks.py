"""An ideal tool sequence must be able to pass every real benchmark task."""

import json

from a3_research_agent.benchmark import real


def task(task_id):
    return next(item for item in real.TASKS if item.id == task_id)


def workspace(tmp_path):
    root = tmp_path / "root"
    (root / "workspace/results").mkdir(parents=True)
    return root


def write(root, name, value):
    (root / "workspace/results" / name).write_text(json.dumps(value), encoding="utf-8")


def calls(root, entries):
    path = root / "run/conversation"
    path.mkdir(parents=True, exist_ok=True)
    (path / "trajectory.jsonl").write_text(
        "\n".join(json.dumps({"tool": name, "arguments": json.dumps(args)}) for name, args in entries)
        + "\n",
        encoding="utf-8",
    )
    return root / "run"


def test_read_tasks_pass_with_their_own_result_file(tmp_path):
    root = workspace(tmp_path)
    write(root, "bench-baseline.json", {"case": "expert_development", "seeds": [31000, 31001, 31002], "native_successes": 3})
    write(root, "bench-config.json", {"seed_start": 21000, "development_seeds": [31000, 31001, 31002]})
    write(root, "bench-memory.json", {"id": "d9ebbacf33e6", "result": "2x10 5k 11/120, 20k 7/120"})
    write(root, "bench-skill.json", {"skill": "a3-mujoco-autoresearch", "points": ["配对比较", "闭环评测"]})
    for name in ("read_baseline", "read_config", "memory_pair_result", "skill_method"):
        passed, detail = task(name).score(root, {}, root / "run")
        assert passed, (name, detail)


def test_job_results_pass_with_real_shaped_evidence(tmp_path):
    root = workspace(tmp_path)
    job = "a" * 32
    write(root, "bench-job.json", {"job_id": job, "status": "completed", "exit_code": 0})
    write(root, "bench-batch.json", {"jobs": [{"job_id": job, "exit_code": 0}, {"job_id": "b" * 32, "exit_code": 0}]})
    write(root, "bench-failure.json", {"job_id": job, "exit_code": 1, "diagnosis": "CUDA out of memory"})
    write(root, "bench-prepare.json", {"prepared_id": "prepared-" + "c" * 32, "preflight_job_id": job})
    for name in ("cpu_job", "batch_jobs", "job_failure", "gpu_prepare"):
        passed, detail = task(name).score(root, {}, root / "run")
        assert passed, (name, detail)


def test_plan_review_and_add_pass(tmp_path):
    root = workspace(tmp_path)
    plan = real._plan_for(task("plan_add_step"))
    plan["steps"]["plan-added"] = {
        "route": "bench-plan",
        "status": "pending",
        "task": "用 bench 脚本在 CPU 上检查环境",
        "criterion": "命令 exit 0 且结果写入 results/bench-plan.txt",
        "depends_on": [],
        "jobs": [],
        "progress": "",
        "evidence": [],
    }
    passed, detail = task("plan_add_step").score(root, plan, root / "run")
    assert passed, detail

    review_plan = real._plan_for(task("review_result"))
    review_plan["reviews"] = [
        {"route": "bench-review", "metrics": ["expert_development native_successes=3"]}
    ]
    passed, detail = task("review_result").score(root, review_plan, root / "run")
    assert passed, detail


def test_blocked_and_recover_need_the_safety_actions(tmp_path):
    root = workspace(tmp_path)
    write(root, "bench-blocked.json", {"status": "blocked", "reason": "需要操作者开通 8 卡权限"})
    run = calls(root, [("research_status", {"status": "blocked"})])
    passed, detail = task("blocked_report").score(root, {}, run)
    assert passed, detail

    write(root, "bench-recover.json", {"job_id": "9" * 32, "status": "running", "checked": True})
    run = calls(root, [("job_submit", {"command": ["sleep", "30"]}), ("job_list", {})])
    passed, detail = task("recover_submit").score(root, {}, run)
    assert passed, detail

    unsafe = calls(root, [("job_submit", {"command": ["a"]}), ("job_submit", {"command": ["b"]}), ("job_list", {})])
    passed, _ = task("recover_submit").score(root, {}, unsafe)
    assert not passed
