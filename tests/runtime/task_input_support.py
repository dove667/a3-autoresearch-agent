import json
from pathlib import Path


def task_inputs(directory: Path, *, goal="Research the selected question", check="result") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in {
        "goal.md": goal,
        "acceptance.md": f"## {check}. Preserve reproducible evidence",
        "protocol.md": "Record source, configuration, and observations.",
    }.items():
        (directory / name).write_text("Version: 1.0.0\n\n" + content + "\n")
    return directory


def deployment_source(directory: Path) -> Path:
    for relative, marker in {
        "agent-inputs/SOUL.md": "SOUL PRINCIPLES UNIQUE",
        "agent-inputs/shared/execution.md": "## API\nEXECUTION REFERENCE UNIQUE",
        "agent-inputs/shared/workspace.md": "WORKSPACE REFERENCE UNIQUE",
    }.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Version: 1.0.0\n\n" + marker + "\n")
    skill = directory / "skills/a3-mujoco-autoresearch"
    (skill / "assets").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Research method\nSKILL REFERENCE UNIQUE\n")
    (skill / "assets/catalog-entry.json").write_text(
        json.dumps(
            {
                "id": "a3-mujoco-autoresearch",
                "version": "1.0.0",
                "status": "reference",
                "local_source": "skills/a3-mujoco-autoresearch/SKILL.md",
                "description": "Reusable research method",
            }
        )
    )
    return directory
