"""Explicit seed groups advance frozen shards and keep partial results distinct."""

import json

from a3_research_agent.harness import job_tools


def test_explicit_group_runs_noncontiguous_seeds_once_and_aggregates(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "state"))
    jobs = {}
    calls = []

    def request(method, route, body=None, **_):
        if route == "/jobs/observe":
            return {
                "jobs": [
                    {"job_id": job_id, "status": "completed", "exit_code": 0}
                    for job_id in body["job_ids"]
                ]
            }
        if method == "POST":
            assert route == "/jobs"
            job_id = f"{len(jobs) + 1:032x}"
            jobs[job_id] = body
            calls.append(body)
            return {"job_id": job_id, "status": "queued"}
        job_id = route.split("/")[2]
        job = jobs[job_id]
        if "/artifacts?" in route:
            if "a3-preflight.json" in route:
                content = {"schema": 1, "status": "passed"}
            else:
                command = job["command"]
                seed = int(command[command.index("--seed-start") + 1])
                raw = 11 if seed == 10000 else 6
                content = {
                    "schema": 1,
                    "report": job["benchmark_receipt"].replace(".receipt.json", ".json"),
                    "protocol": {"task": "2x5", "episodes": 1, "seed_start": seed},
                    "episodes": [
                        {
                            "seed": seed,
                            "score_raw": raw,
                            "score_capped": min(raw, 10),
                            "max_score": 10,
                            "steps": 500,
                            "success": False,
                        }
                    ],
                }
            return {"encoding": "utf-8", "eof": True, "content": json.dumps(content)}
        return {"job_id": job_id, "status": "completed", "exit_code": 0}

    args = {"checkpoint": "/models/act", "task": "2x5", "seeds": [10013, 10000]}
    created = job_tools.dispatch("benchmark", args, request, workspace=tmp_path)
    assert [row["seed"] for row in created["shards"]] == [10000, 10013]
    assert all(not job["gpu"] for job in calls)
    replay = job_tools.dispatch(
        "benchmark", {**args, "seeds": [10000, 10013]}, request, workspace=tmp_path
    )
    assert replay == created and len(calls) == 2
    status = {"group_id": created["group_id"]}
    active = job_tools.dispatch("benchmark_group", status, request, workspace=tmp_path)
    assert active["status"] == "active" and all(job["gpu"] for job in calls[2:])
    results = job_tools.dispatch("benchmark_group", status, request, workspace=tmp_path)
    assert results["status"] == "completed"
    assert results["totals"] == {
        "scored_seeds": 2,
        "expected_seeds": 2,
        "raw_score": 17,
        "capped_score": 16,
        "maximum": 20,
        "passes": 0,
    }
    assert len(calls) == 4


def test_explicit_group_rejects_duplicate_seeds_before_preflight(tmp_path):
    result = job_tools.dispatch(
        "benchmark",
        {"checkpoint": "/models/act", "task": "2x5", "seeds": [7, 7]},
        lambda *_: None,
        workspace=tmp_path,
    )
    assert result["error"] == "invalid_seeds"
