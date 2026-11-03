"""G task files use the generic retained-task input contract, not a new profile."""

import json
from pathlib import Path

from runtime.local.continuation import HarnessStart
from runtime.local.input_versions import InputVersions, input_sources
from runtime.local.launch import prepare_task_start

ROOT = Path(__file__).resolve().parents[2]


def test_context_task_bundle_loads_g_and_initial_notice(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    sources = input_sources("phase-one")
    sources.update({
        "goal.md": "agent-inputs/phase-one-context/goal.md",
        "acceptance.md": "agent-inputs/phase-one-context/acceptance.md",
        "phase-instructions.md": "agent-inputs/phase-one-context/instructions.md",
        "initial-feedback.md": "agent-inputs/phase-one-context/initial-feedback.md",
    })
    for name, source in sources.items():
        (inputs / name).write_bytes((ROOT / source).read_bytes())
    versions = InputVersions(ROOT, tmp_path / "run", custom_dir=inputs)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert manifest["input_mode"] == "custom"
    assert manifest["acceptance_checks"] == ["G"]
    assert manifest["task_profile"] == "standard"
    assert manifest["files"]["SOUL.md"]["version"] == "0.2.3"
    _, instructions = versions.model_instructions()
    assert proposal["contents"]["SOUL.md"] in instructions
    assert proposal["contents"]["phase-instructions.md"] in instructions
    assert proposal["contents"]["goal.md"] in instructions
    assert proposal["contents"]["acceptance.md"] in instructions
    assert "protocol.md" not in proposal["contents"]
    assert (ROOT / "agent-inputs/phase-two/goal.md").read_text() not in instructions
    assert manifest["acceptance_checks"] == ["G"]
    assert all(item["version"] for item in manifest["files"].values())
    source = HarnessStart(
        ["python", "agent.py", "--resume"],
        {
            "agent.py": "inherited harness code",
            "checkpoint.json": json.dumps([{"role": "user", "content": "old F"}]),
            "run-outcome.json": '{"status":"complete","acceptance":{"F":{"status":"passed"}}}',
            "memory.jsonl": "inherited confirmed conclusions",
        },
        Path("/source/F-run"),
        "ready-source-version",
    )
    before = dict(source.files)
    fresh, context = prepare_task_start(
        source, "phase-one", "standard", proposal["contents"]["goal.md"],
        custom_inputs=True,
        initial_feedback=proposal["contents"]["initial-feedback.md"],
    )
    messages = json.loads(fresh.files["checkpoint.json"])
    assert len(messages) == 2
    assert messages[0]["content"] == proposal["contents"]["goal.md"]
    assert proposal["contents"]["initial-feedback.md"] in messages[1]["content"]
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    assert context["initial_feedback"]["mode"] == "initial-user-input"
    assert fresh.files["memory.jsonl"] == before["memory.jsonl"]
    assert source.files == before
