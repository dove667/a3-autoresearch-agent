"""A cell total must be capped, comparable, and honest about its segments."""

import json

from a3_research_agent.harness import benchmark_report

JOB_A = "a" * 32
JOB_B = "b" * 32


def pages(text, size=40):
    """Serve a report through the same character pages the artifact route returns."""
    chunks = [text[index : index + size] for index in range(0, len(text), size)] or [""]
    return chunks


def request_for(text, size=40):
    chunks = pages(text, size)
    seen = {"offset": 0}

    def request(method, path):
        offset = int(path.split("offset=")[1].split("&")[0])
        index = offset // size
        content = chunks[index] if index < len(chunks) else ""
        end = min(offset + len(content), len(text))
        seen["offset"] = end
        return {
            "encoding": "utf-8",
            "content": content,
            "bytes": len(text),
            "end": end,
            "eof": end >= len(text),
        }

    return request


def report(task="2x5", scores=(10, 11), successes=(True, False), steps=400, **run):
    episodes = [
        {
            "seed": 10000 + index,
            "score": score,
            "max_score": 10 if task == "2x5" else 20,
            "cookies_in_target": score,
            "success": successes[index],
            "steps": steps,
        }
        for index, score in enumerate(scores)
    ]
    return {
        "run": {
            "task": task,
            "profile": run.get("profile", "basic"),
            "max_steps": run.get("max_steps", 500),
            "n_action_steps": run.get("n_action_steps", 25),
            "prompt_style": run.get("prompt_style", "training"),
            "weights_sha256": run.get("weights_sha256", "f" * 64),
        },
        "episodes": episodes,
    }


def summarise(reports, job_requests):
    return benchmark_report.summary(
        lambda method, path: job_requests[path.split("/")[2]](method, path),
        {"reports": reports},
    )


def test_scores_are_capped_and_passes_are_separate():
    text = json.dumps(report())
    result = summarise(
        [{"job_id": JOB_A, "path": "cell/seed-10000.json"}],
        {JOB_A: request_for(text)},
    )
    assert result["in_box_raw"] == 21
    assert result["in_box"] == 20
    assert result["max_possible"] == 20
    assert result["passed"] == 1
    assert result["pass_rate"] == 0.5
    assert any("exceeds the 20" in warning for warning in result["warnings"])


def test_segments_with_different_budgets_are_flagged():
    first = json.dumps(report(max_steps=500))
    second = json.dumps(report(max_steps=3200, scores=(5, 5), successes=(False, False)))
    result = summarise(
        [
            {"job_id": JOB_A, "path": "cell/a.json"},
            {"job_id": JOB_B, "path": "cell/b.json"},
        ],
        {JOB_A: request_for(first), JOB_B: request_for(second)},
    )
    assert result["episodes"] == 4
    assert any("mixed max_steps" in warning for warning in result["warnings"])
    assert result["protocol"]["max_steps"] == ["3200", "500"]


def test_duplicate_seeds_are_flagged_because_the_sum_double_counts():
    text = json.dumps(report(scores=(5, 5), successes=(False, False)))
    result = summarise(
        [
            {"job_id": JOB_A, "path": "cell/a.json"},
            {"job_id": JOB_B, "path": "cell/b.json"},
        ],
        {JOB_A: request_for(text), JOB_B: request_for(text)},
    )
    assert any("duplicate seeds" in warning for warning in result["warnings"])


def test_a_report_is_read_through_several_pages():
    text = json.dumps(report(scores=tuple(range(1, 11)), successes=(False,) * 10))
    assert len(text) > 40
    result = summarise(
        [{"job_id": JOB_A, "path": "cell/seed-10000.json"}],
        {JOB_A: request_for(text, size=40)},
    )
    assert result["episodes"] == 10
    assert result["in_box"] == 55


def test_missing_report_returns_an_actionable_error():
    def request(method, path):
        return {"http_error": 404}

    try:
        benchmark_report.summary(request, {"reports": [{"job_id": JOB_A, "path": "cell/x.json"}]})
    except Exception as error:  # noqa: BLE001 - the tool layer maps this to a hint
        assert getattr(error, "code", "") == "benchmark_report_unreadable"
    else:
        raise AssertionError("a missing report must not produce a summary")


def test_registered_receipt_can_be_reused_without_a_guessed_report_path():
    receipt_path = "cell/seed-10000.receipt.json"
    document = {
        "schema": 1,
        "report": "cell/seed-10000.json",
        "protocol": {"task": "2x5", "episodes": 1, "seed_start": 10000},
        "episodes": [
            {
                "seed": 10000,
                "score_raw": 11,
                "score_capped": 10,
                "max_score": 10,
                "steps": 500,
                "success": False,
            }
        ],
    }
    artifact = request_for(json.dumps(document))

    def request(method, path):
        if path == f"/jobs/{JOB_A}":
            return {"benchmark_receipt": receipt_path}
        assert receipt_path in path or receipt_path.replace("/", "%2F") in path
        return artifact(method, path)

    result = benchmark_report.summary(request, {"reports": [{"job_id": JOB_A}]})
    assert (result["in_box_raw"], result["in_box"], result["passed"]) == (11, 10, 0)
