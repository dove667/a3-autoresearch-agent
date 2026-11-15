"""Fresh native research reads hash-pinned shared research files."""

import json

from native_run_support import native_run

from a3_research_agent.host.inputs.research_preparation import prepare_research_start
from a3_research_agent.host.runs.continuation import load_run_result, publish_run_result


def test_repeated_fresh_research_uses_the_same_verified_shared_files(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    publish_run_result(run, workspace, "main")
    source = load_run_result(run)
    before = dict(source.files)
    first, metadata = prepare_research_start(source)
    second, _ = prepare_research_start(source)
    assert json.loads(first.files["checkpoint.json"]) == []
    assert json.loads(second.files["checkpoint.json"]) == []
    assert (
        first.files["experiments/lesson.md"]
        == second.files["experiments/lesson.md"]
        == before["experiments/lesson.md"]
    )
    assert source.files == before
    assert metadata["mode"] == "fresh"
    assert metadata["source_commit"] == source.harness_commit
    assert json.loads(first.files["run-outcome.json"])["status"] == "continue"


def test_large_shared_text_prepares_and_materializes_without_job_upload_limits(tmp_path):
    from a3_research_agent.host.sessions.workspace import write_workspace_files

    run, workspace, _, _, _ = native_run(tmp_path)
    reports = {
        f"experiments/training/report-{index}.txt": str(index) + "x" * 170000
        for index in range(129)
    }
    assert sum(len(content.encode()) for content in reports.values()) > 20 * 1024 * 1024
    write_workspace_files(workspace, reports)
    declaration = run / "conversation/continuation.json"
    manifest = json.loads(declaration.read_text())
    manifest["files"].extend(reports)
    declaration.write_text(json.dumps(manifest))

    result = publish_run_result(run, workspace, "main")
    assert result["status"] == "ready"
    source = load_run_result(run)
    prepared, _ = prepare_research_start(source)
    assert reports.items() <= prepared.files.items()
    destination = tmp_path / "new-workspace"
    destination.mkdir()
    research_files = {
        name: content
        for name, content in prepared.files.items()
        if name not in {"checkpoint.json", "run-outcome.json"}
    }
    write_workspace_files(destination, research_files)
    for name, content in reports.items():
        assert (destination / name).read_text() == content
