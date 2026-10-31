import shutil
import sys
from pathlib import Path

import pytest

from runtime.remote import prepare_image


def reference_project(project: Path) -> dict[str, bytes]:
    files = {
        "README.md": b"See docs/workflow_guide.md and examples/benchmark_cookie_batch.py\r\n",
        "docs/workflow_guide.md": b"python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli\n",
        "docs/contracts.md": b"action contract\n",
        "examples/benchmark_cookie_batch.py": b"print('benchmark entry')\n",
        "examples/helper.py": b"print('helper')\n",
    }
    for name, data in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return files


def test_image_references_include_documented_entry_without_private_files(tmp_path):
    project = tmp_path / "project"
    files = reference_project(project)
    (project / "docs/.env").write_text("PRIVATE_FIXTURE")
    (project / "examples/__pycache__").mkdir()
    (project / "examples/__pycache__/entry.pyc").write_bytes(b"cache")
    destination = tmp_path / "context/a3"
    prepare_image.copy_public_references(project, destination)
    assert {
        str(p.relative_to(destination)): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    } == files


@pytest.mark.parametrize(
    "missing", ["docs/workflow_guide.md", "docs/contracts.md", "examples/benchmark_cookie_batch.py"]
)
def test_image_build_rejects_missing_required_reference_files(tmp_path, missing):
    project = tmp_path / "project"
    reference_project(project)
    (project / missing).unlink()
    destination = tmp_path / "context/a3"
    with pytest.raises(ValueError, match="Missing A3 reference"):
        prepare_image.copy_public_references(project, destination)
    assert not destination.exists()


def test_full_build_stages_references_before_invoking_docker(tmp_path, monkeypatch):
    project = tmp_path / "project"
    files = reference_project(project)
    interpreter = project / "interpreter/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("interpreter fixture")
    (project / ".venv/bin").mkdir(parents=True)
    (project / ".venv/bin/python").symlink_to(interpreter)
    (project / ".runtime/ffmpeg").mkdir(parents=True)
    context = tmp_path / "context"
    builds = []

    def execute(command, **kwargs):
        if command[0] == "cp":
            source, destination = map(Path, command[-2:])
            shutil.copytree(source, destination, symlinks=True)
        else:
            assert command[:2] == ["docker", "build"]
            builds.append(command)
            for name, data in files.items():
                assert (context / "a3" / name).read_bytes() == data

    monkeypatch.setattr(prepare_image.subprocess, "run", execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_image",
            "--project",
            str(project),
            "--context",
            str(context),
            "--image",
            "fixture/runtime",
        ],
    )
    prepare_image.main()
    assert len(builds) == 1
