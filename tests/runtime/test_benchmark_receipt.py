"""Benchmark receipts use exact scores and synchronized trace evidence."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from a3_research_agent.harness import benchmark_runner, waiting
from a3_research_agent.harness.tool_contracts import ToolInputError


@pytest.mark.parametrize("diagnostics", [False, True])
def test_runner_publishes_per_seed_receipt(tmp_path, monkeypatch, diagnostics):
    args = benchmark_runner.parser().parse_args(
        [
            "--task",
            "2x10",
            "--policy",
            "act:/checkpoint",
            "--output-dir",
            "cell",
            "--episodes",
            "1",
            "--seed-start",
            "10003",
        ]
    )
    args.diagnostics = diagnostics
    monkeypatch.setenv("A3_OUTPUTS", str(tmp_path))

    steps = [
        {
            "cookies_in_source": 2,
            "cookies_in_source_mask": [True, True, False],
            "cookies_in_target": 1,
            "cookies_in_target_mask": [False, False, True],
            "contacts": [{"geom1": "target_bin_back_wall", "geom2": "cookie_2_geom"}],
        },
        {
            "cookies_in_source": 1,
            "cookies_in_source_mask": [False, True, False],
            "cookies_in_target": 1,
            "cookies_in_target_mask": [False, False, True],
            "contacts": [{"geom1": "cookie_0_top_geom", "geom2": "target_bin_back_wall"}],
        },
        {
            "cookies_in_source": 1,
            "cookies_in_source_mask": [False, True, False],
            "cookies_in_target": 1,
            "cookies_in_target_mask": [True, False, False],
            "contacts": [{"geom1": "target_bin_back_wall", "geom2": "cookie_0_geom"}],
            "count_fill": False,
            "success": False,
            "success_hold_count": 0,
            "unmet_success_conditions": ["count_fill", "hold_steps"],
        },
    ]

    def record(call):
        argv = call["arguments"]
        output = Path(argv[argv.index("--output") + 1])
        run = {"task": "2x10", "max_steps": 3200, "weights_sha256": "a" * 64}
        episodes = [
            {"seed": 10003, "cookies_in_target": 21, "max_score": 20, "success": False, "steps": 3}
        ]
        output.write_text(json.dumps({"run": run, "episodes": episodes}))
        if diagnostics:
            directory = output.parent / "diagnostics"
            trace, videos = benchmark_runner.diagnostic_artifacts(directory, 10003)
            trace.parent.mkdir(parents=True)
            trace.write_text(
                "".join(
                    json.dumps({"step": step, "after": state}) + "\n"
                    for step, state in enumerate(steps)
                )
            )
            for video in videos.values():
                video.write_bytes(b"recording")
            (directory / "manifest.json").write_text(json.dumps(run))
        return 0

    monkeypatch.setattr(benchmark_runner, "run_benchmark", record)
    assert benchmark_runner.run(args) == 0
    receipt = json.loads((tmp_path / "cell/seed-10003.receipt.json").read_text())
    assert receipt["report"] == "cell/seed-10003.json"
    assert receipt["protocol"]["weights_sha256"] == "a" * 64
    [episode] = receipt["episodes"]
    assert [
        episode[field]
        for field in ("seed", "score_raw", "score_capped", "max_score", "success", "steps")
    ] == [
        10003,
        21,
        20,
        20,
        False,
        3,
    ]
    if diagnostics:
        events = episode["trace_events"]
        assert episode["trace"] == "cell/diagnostics/seed_10003/trace.jsonl"
        assert episode["videos"]["front"] == "cell/diagnostics/seed_10003/front.mp4"
        assert json.loads((tmp_path / episode["events"]).read_text()) == events
        assert events["step_index_convention"] == "zero-based"
        assert events["changes"] == [
            {"step": 1, "source": {"count": 1, "exited": [0]}},
            {"step": 2, "target": {"count": 1, "entered": [0], "exited": [2]}},
        ]
        assert events["first_target_contacts"] == [
            {"step": 0, "surface": "target_bin_back_wall", "cookies": [2]},
            {"step": 1, "surface": "target_bin_back_wall", "cookies": [0]},
        ]
        assert events["final"]["unmet_success_conditions"] == ["count_fill", "hold_steps"]
    else:
        assert "trace_events" not in episode


def test_completed_wait_includes_exact_benchmark_receipt():
    job = "a" * 32
    path = "cell/seed-10003.receipt.json"
    receipt = {
        "schema": 1,
        "report": "cell/seed-10003.json",
        "protocol": {"task": "2x10", "max_steps": 3200, "seed_start": 10003, "episodes": 1},
        "episodes": [
            {
                "seed": 10003,
                "score_raw": 21,
                "score_capped": 20,
                "max_score": 20,
                "steps": 3200,
                "success": False,
            }
        ],
    }
    raw = json.dumps(receipt)

    def request(method, route, *args, **kwargs):
        if method == "POST":
            return {
                "reason": "terminal",
                "jobs": [{"job_id": job, "status": "completed", "benchmark_receipt": path}],
            }
        if urlsplit(route).path.endswith("/list"):
            return {"entries": ["cell"]}
        query = parse_qs(urlsplit(route).query)
        assert query["path"] == [path]
        return {"encoding": "utf-8", "bytes": len(raw), "content": raw, "eof": True}

    result = waiting.wait_for_jobs(request, [job])
    row = result["changes"][f"/jobs/{job}"]
    assert row["benchmark_receipt"] == path
    assert row["benchmark"] == receipt


def test_receipt_rejects_an_incorrect_capped_score():
    from a3_research_agent.harness.benchmark_report import read_receipt

    payload = {
        "schema": 1,
        "report": "cell/seed-10003.json",
        "protocol": {"seed_start": 10003, "episodes": 1},
        "episodes": [
            {
                "seed": 10003,
                "score_raw": 21,
                "score_capped": 21,
                "max_score": 20,
                "steps": 3200,
                "success": False,
            }
        ],
    }
    raw = json.dumps(payload)

    def request(*args):
        return {"encoding": "utf-8", "bytes": len(raw), "content": raw, "eof": True}

    with pytest.raises(ToolInputError, match="benchmark_receipt_invalid"):
        read_receipt(request, "a" * 32, "cell/seed-10003.receipt.json")
