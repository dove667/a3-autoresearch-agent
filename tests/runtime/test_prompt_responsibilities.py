"""Task instructions and discoverable references have separate consumers."""

import json
import re
from pathlib import Path

from a3_research_agent.host.inputs.versions import InputVersions, deployment_input_sources

ROOT = Path(__file__).resolve().parents[2]


def test_documented_status_example_uses_the_controlled_tool(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    execution = (ROOT / "prompt/execution.md").read_text()
    examples = [
        json.loads(body) for body in re.findall(r"```json\n(.*?)\n```", execution, re.DOTALL)
    ]
    arguments = next(example for example in examples if example.get("status") == "continue")
    assert agent.tool("research_status", arguments)["status"] == "continue"
    saved = tmp_path / "conversation/run-outcome.json"
    assert json.loads(saved.read_text())["reason"] == arguments["reason"]
    assert not list(workspace.iterdir())


def test_operator_documents_are_not_selected_as_agent_inputs(tmp_path):
    versions = InputVersions(ROOT, tmp_path / "run")
    proposal = versions.prepare()
    assert not any(
        source.startswith(("docs/", "archive/"))
        for source in deployment_input_sources(ROOT).values()
    )
    assert not any(name.startswith(("docs/", "archive/")) for name in proposal["files"])
    versions.activate(proposal, "start")
    instructions = versions.model_instructions()[1]
    assert instructions.count(versions.contents["SOUL.md"]) == 1
    assert not {"goal.md", "acceptance.md", "protocol.md"} & proposal["files"].keys()
    assert not any(name.startswith("skills/") for name in proposal["files"])
    for name in (
        "execution.md",
        "workspace-instructions.md",
    ):
        assert versions.contents[name] not in instructions
        assert (versions.run / "inputs" / name).read_text() == versions.contents[name]
