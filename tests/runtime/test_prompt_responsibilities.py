"""Task instructions and discoverable references have separate consumers."""

from pathlib import Path

from task_input_support import task_inputs

from a3_research_agent.host.inputs.versions import InputVersions, deployment_input_sources

ROOT = Path(__file__).resolve().parents[2]


def test_operator_documents_are_not_selected_as_agent_inputs(tmp_path):
    task = task_inputs(tmp_path / "task", goal="The selected current research question")
    versions = InputVersions(ROOT, tmp_path / "run", custom_dir=task)
    proposal = versions.prepare()
    assert not any(
        source.startswith(("docs/", "archive/"))
        for source in deployment_input_sources(ROOT).values()
    )
    assert not any(name.startswith(("docs/", "archive/")) for name in proposal["files"])
    versions.activate(proposal, "start")
    instructions = versions.model_instructions()[1]
    for name in ("SOUL.md", "goal.md", "acceptance.md", "protocol.md"):
        assert instructions.count(versions.contents[name]) == 1
    for name in (
        "execution.md",
        "workspace-instructions.md",
        "skills/a3-mujoco-autoresearch/SKILL.md",
    ):
        assert versions.contents[name] not in instructions
        assert (versions.run / "inputs" / name).read_text() == versions.contents[name]


def test_independent_tasks_receive_only_their_selected_contract(tmp_path):
    first = task_inputs(tmp_path / "first-task", goal="Unique question A", check="first")
    second = task_inputs(tmp_path / "second-task", goal="Unique question B", check="second")
    prompts = []
    for index, directory in enumerate((first, second)):
        versions = InputVersions(ROOT, tmp_path / f"run-{index}", custom_dir=directory)
        versions.activate(versions.prepare(), "start")
        prompts.append(versions.model_instructions()[1])
    assert "Unique question A" in prompts[0] and "Unique question B" not in prompts[0]
    assert "Unique question B" in prompts[1] and "Unique question A" not in prompts[1]
