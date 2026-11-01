"""Keep stage goals and model instructions isolated across start and resume."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.input_versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("phase", ["phase-one", "phase-two"])
def test_only_selected_phase_enters_model_request(tmp_path, phase):
    versions = InputVersions(ROOT, tmp_path / "run", phase=phase)
    manifest = versions.activate(versions.prepare(), "start")
    calls = []
    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"token": "main"},
            main_owner="main",
            forward_model=lambda **kwargs: calls.append(kwargs) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            input_versions=versions,
        )
    )
    result = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer token"},
        json={"input": "Continue", "instructions": "Own instructions"},
    )
    assert result.status_code == 200
    assert manifest["phase"] == phase
    files = manifest["files"]
    assert files["phase-instructions.md"]["source"] == f"configs/prompts/{phase}.md"
    assert files["goal.md"]["source"] == f"evals/{phase}/goal.md"
    assert "common-instructions.md" not in files
    instructions = calls[-1]["instructions"]
    assert (ROOT / f"configs/prompts/{phase}.md").read_text() in instructions
    other = "phase-one" if phase == "phase-two" else "phase-two"
    assert (ROOT / f"configs/prompts/{other}.md").read_text() not in instructions
    if phase == "phase-two":
        assert (ROOT / "evals/phase-two/protocol.md").read_text() in instructions
    else:
        assert "protocol.md" not in files


def test_resume_and_hot_reload_preserve_run_phase(tmp_path):
    run = tmp_path / "run"
    first = InputVersions(ROOT, run, phase="phase-two")
    first.activate(first.prepare(), "start")
    restored = InputVersions(ROOT, run)
    proposal = restored.prepare()
    assert proposal["phase"] == "phase-two"
    restored.activate(proposal, "resume")
    assert restored.prepare()["phase"] == "phase-two"
    with pytest.raises(ValueError, match="phase"):
        InputVersions(ROOT, run, phase="phase-one")
    with pytest.raises(ValueError, match="phase"):
        restored.activate(InputVersions(ROOT, tmp_path / "other").prepare(), "hot_reload")


def test_older_run_without_phase_uses_phase_one(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "startup.json").write_text(json.dumps({"run_id": run.name}))
    versions = InputVersions(ROOT, run)
    assert versions.prepare()["phase"] == "phase-one"


def test_retired_common_prompt_is_archived_and_removed_from_active_mounts(tmp_path):
    run = tmp_path / "run"
    for name in ("inputs", "private-inputs"):
        mount = run / name
        mount.mkdir(parents=True)
        (mount / "common-instructions.md").write_text("old prompt")
    versions = InputVersions(ROOT, run)
    versions.activate(versions.prepare(), "resume")
    assert (run / "input-revisions/legacy/common-instructions.md").read_text() == "old prompt"
    for name in ("inputs", "private-inputs"):
        assert not (run / name / "common-instructions.md").exists()
        assert (run / name / "phase-instructions.md").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--phase", "phase-two"],
        ["--phase", "phase-two", "--verify-deployment"],
    ],
)
def test_incomplete_stage_selection_fails_before_deployment(tmp_path, monkeypatch, arguments):
    import sys

    from runtime.local import launch

    monkeypatch.setattr(sys, "argv", ["launch.py", *arguments])
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("deployment reached"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2
