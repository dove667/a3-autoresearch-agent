"""Patch injected Controller dependencies at their component lookup sites."""

import importlib

COMPONENTS = (
    "controller.remote_jobs",
    "controller.files",
    "controller.store",
    "controller.docker",
    "controller.lifecycle",
    "controller.artifacts",
    "controller.submission",
    "controller.cli",
    "scheduling.gpu",
    "exports.operations",
)


def patch_controller(monkeypatch, facade, name, replacement):
    """Preserve each test's fake backend across component import bindings."""
    assert hasattr(facade, name), name
    for component in COMPONENTS:
        module = importlib.import_module(f"a3_research_agent.execution.{component}")
        if hasattr(module, name):
            monkeypatch.setattr(module, name, replacement)
