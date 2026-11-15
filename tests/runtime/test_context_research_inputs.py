"""New research contexts carry selected knowledge without old task prompts."""

import hashlib
import json
from pathlib import Path

import pytest

from a3_research_agent.host.inputs.research_preparation import input_hashes, prepare_research_start
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.runs.continuation import HarnessStart

ROOT = Path(__file__).resolve().parents[2]


def test_new_research_preserves_knowledge_and_starts_an_empty_model_context(tmp_path):
    source = HarnessStart(
        COMMAND.copy(),
        {
            "checkpoint.json": json.dumps([{"role": "user", "content": "previous task"}]),
            "experiments/lesson.md": "Verified reusable lesson\n",
            "experiments/manifest.json": '{"observations":3}',
        },
        previous_run=tmp_path / "source-run",
        harness_commit="a" * 40,
    )
    before = dict(source.files)
    fresh, context = prepare_research_start(source)
    assert json.loads(fresh.files["checkpoint.json"]) == []
    assert "task.txt" not in fresh.files
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    assert context["initial_message_count"] == 0
    assert fresh.files["experiments/lesson.md"] == before["experiments/lesson.md"]
    assert fresh.files["experiments/manifest.json"] == before["experiments/manifest.json"]
    assert source.files == before


def test_research_hashes_bind_actual_selected_input_bytes(tmp_path):
    inputs = InputVersions(ROOT, tmp_path / "run")
    manifest = inputs.activate(inputs.prepare(), "start")
    selected = inputs.run / "inputs"
    assert (
        input_hashes(selected)["SOUL.md"]
        == hashlib.sha256((selected / "SOUL.md").read_bytes()).hexdigest()
    )
    assert input_hashes(selected)["execution.md"] == manifest["files"]["execution.md"]["sha256"]
    (selected / "execution.md").write_text("changed after selection")
    with pytest.raises(ValueError, match="active manifest"):
        input_hashes(selected)


def test_fresh_research_rejects_the_retired_research_directory(tmp_path):
    source = HarnessStart(
        COMMAND.copy(),
        {"checkpoint.json": "[]", "research/train.py": "print('old path')"},
        harness_commit="a" * 40,
    )
    with pytest.raises(ValueError, match="only retained knowledge"):
        prepare_research_start(source)
