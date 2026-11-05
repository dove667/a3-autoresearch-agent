"""Exercise Host component imports and explicit project paths."""

import importlib
from pathlib import Path

import pytest


def test_workspace_validation_is_a_leaf_and_rejects_symlinks(tmp_path):
    from a3_research_agent.workspace.paths import workspace_path

    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    for name in ("../escape", "/absolute", "alias/file"):
        with pytest.raises(ValueError):
            workspace_path(tmp_path, name)
    assert workspace_path(tmp_path, "normal/file") == tmp_path / "normal/file"


def test_project_root_is_explicit_and_independent_of_simulation_setting(tmp_path, monkeypatch):
    from a3_research_agent.project import resolve_project_root

    monkeypatch.setenv("A3_PROJECT_ROOT", "/simulation/not/infrastructure")
    assert resolve_project_root(tmp_path) == tmp_path.resolve()
    with pytest.raises(ValueError, match="project root"):
        resolve_project_root(tmp_path / "missing")


def test_host_app_factory_remains_importable():
    from a3_research_agent.host.gateway.app import create_app

    assert callable(create_app)
    assert Path(importlib.import_module(create_app.__module__).__file__).parts[-2] == "gateway"
