"""Task instructions and discoverable references have separate consumers."""

import json
import re
from pathlib import Path

from a3_research_agent.host.inputs.versions import InputVersions, deployment_input_sources
from a3_research_agent.host.sessions.packages import read_continuation

ROOT = Path(__file__).resolve().parents[2]


def test_documented_continuation_uses_the_runnable_native_contract(tmp_path):
    execution = (ROOT / "prompt/execution.md").read_text()
    examples = [
        json.loads(body) for body in re.findall(r"```json\n(.*?)\n```", execution, re.DOTALL)
    ]
    manifest = next(example for example in examples if "remote_exports" in example)
    (tmp_path / "continuation.json").write_text(json.dumps(manifest))
    for name in manifest["files"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("[]" if name == ".agent/checkpoint.json" else "")
    assert read_continuation(tmp_path) == manifest


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
    for name in (
        "execution.md",
        "workspace-instructions.md",
        "skills/a3-mujoco-autoresearch/SKILL.md",
    ):
        assert versions.contents[name] not in instructions
        assert (versions.run / "inputs" / name).read_text() == versions.contents[name]
