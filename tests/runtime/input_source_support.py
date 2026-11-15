import json
from pathlib import Path


def deployment_source(directory: Path) -> Path:
    for relative, marker in {
        "prompt/SOUL.md": "SOUL PRINCIPLES UNIQUE",
        "prompt/execution.md": "## API\nEXECUTION REFERENCE UNIQUE",
        "prompt/workspace.md": "WORKSPACE REFERENCE UNIQUE",
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
