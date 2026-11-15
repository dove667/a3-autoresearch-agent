"""Fresh native research shares immutable retained knowledge."""

import json

from native_run_support import native_run

from a3_research_agent.host.inputs.research_preparation import prepare_research_start
from a3_research_agent.host.runs.continuation import load_run_result, publish_run_result


def test_repeated_fresh_research_shares_immutable_source_knowledge(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    publish_run_result(run, workspace, "main")
    source = load_run_result(run)
    before = dict(source.files)
    first, metadata = prepare_research_start(source)
    second, _ = prepare_research_start(source)
    assert json.loads(first.files[".agent/checkpoint.json"]) == []
    assert json.loads(second.files[".agent/checkpoint.json"]) == []
    assert (
        first.files["research/lesson.md"]
        == second.files["research/lesson.md"]
        == before["research/lesson.md"]
    )
    assert source.files == before
    assert metadata["mode"] == "fresh"
    assert metadata["source_commit"] == source.harness_commit
    assert json.loads(first.files["run-outcome.json"])["status"] == "continue"
