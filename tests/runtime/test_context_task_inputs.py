"""Explicit new tasks carry research knowledge into a fresh task context."""

import hashlib
import json
from pathlib import Path

import pytest
from task_input_support import task_inputs

from a3_research_agent.host.inputs.task_preparation import input_hashes, prepare_task_start
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.runs.continuation import HarnessStart

ROOT = Path(__file__).resolve().parents[2]


def test_selected_task_and_initial_feedback_replace_previous_model_context(tmp_path):
    directory = task_inputs(tmp_path / "task", goal="Analyze the current result", check="context")
    notice = "Version: 1.0.0\nUse the reviewed experiment manifest.\n"
    (directory / "initial-feedback.md").write_text(notice)
    inputs = InputVersions(ROOT, tmp_path / "run", custom_dir=directory)
    manifest = inputs.activate(inputs.prepare(), "start")
    assert manifest["acceptance_checks"] == ["context"]
    source = HarnessStart(
        COMMAND.copy(),
        {
            "checkpoint.json": json.dumps([{"role": "user", "content": "previous task"}]),
            "memory.jsonl": '{"id":"known","text":"verified conclusion"}\n',
            "research/manifest.json": '{"observations":3}',
        },
        previous_run=tmp_path / "source-run",
        harness_commit="a" * 40,
    )
    before = dict(source.files)
    fresh, context = prepare_task_start(
        source,
        inputs.contents["goal.md"],
        initial_feedback=notice,
        initial_feedback_source=directory / "initial-feedback.md",
    )
    messages = json.loads(fresh.files["checkpoint.json"])
    assert len(messages) == 2 and messages[0]["content"] == inputs.contents["goal.md"]
    assert notice in messages[1]["content"]
    assert "previous task" not in json.dumps(messages)
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    assert context["initial_feedback"]["mode"] == "initial-user-input"
    assert fresh.files["memory.jsonl"] == before["memory.jsonl"]
    assert fresh.files["research/manifest.json"] == before["research/manifest.json"]
    assert source.files == before


def test_task_hashes_bind_actual_selected_input_bytes(tmp_path):
    inputs = InputVersions(ROOT, tmp_path / "run", custom_dir=task_inputs(tmp_path / "task"))
    manifest = inputs.activate(inputs.prepare(), "start")
    selected = inputs.run / "inputs"
    assert (
        input_hashes(selected)["goal.md"]
        == hashlib.sha256((selected / "goal.md").read_bytes()).hexdigest()
    )
    assert input_hashes(selected)["protocol.md"] == manifest["files"]["protocol.md"]["sha256"]
    (selected / "protocol.md").write_text("changed after selection")
    with pytest.raises(ValueError, match="active manifest"):
        input_hashes(selected)
