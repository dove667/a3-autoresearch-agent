"""Explicit fresh native tasks reuse frozen knowledge without old task state."""

import json

from native_run_support import native_run

from a3_research_agent.host.inputs.task_preparation import prepare_task_start
from a3_research_agent.host.runs.continuation import load_run_result, publish_run_result


def test_repeated_fresh_tasks_share_immutable_source_knowledge(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    publish_run_result(run, workspace, "main")
    source = load_run_result(run)
    before = dict(source.files)
    first, metadata = prepare_task_start(
        source, "new goal", initial_feedback="Use the verified dataset."
    )
    second, _ = prepare_task_start(source, "another goal")
    assert json.loads(first.files["checkpoint.json"])[0]["content"] == "new goal"
    assert len(json.loads(first.files["checkpoint.json"])) == 2
    assert json.loads(second.files["checkpoint.json"]) == [
        {"role": "user", "content": "another goal"}
    ]
    assert first.files["memory.jsonl"] == second.files["memory.jsonl"] == before["memory.jsonl"]
    assert source.files == before
    assert metadata["mode"] == "fresh"
    assert metadata["source_commit"] == source.harness_commit
    assert json.loads(first.files["run-outcome.json"])["status"] == "continue"
