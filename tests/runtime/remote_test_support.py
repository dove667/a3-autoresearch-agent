"""Patch injected Controller dependencies at their component lookup sites."""

import importlib

COMPONENTS = (
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


def patch_controller(monkeypatch, name, replacement):
    """Preserve each test's fake backend across component import bindings."""
    patched = []
    for component in COMPONENTS:
        module = importlib.import_module(f"a3_research_agent.execution.{component}")
        if hasattr(module, name):
            monkeypatch.setattr(module, name, replacement)
            patched.append(component)
    assert patched, name
