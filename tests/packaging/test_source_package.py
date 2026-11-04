"""Exercise canonical source imports without importing operational entry points."""

import importlib
from pathlib import Path


def test_canonical_package_is_importable_from_src():
    package = importlib.import_module("a3_research_agent")
    assert Path(package.__file__).resolve().parent == (
        Path(__file__).resolve().parents[2] / "src/a3_research_agent"
    )


def test_legacy_alias_exposes_one_module_instance():
    from a3_research_agent._compat import redirect

    namespace = {"__name__": "legacy_test_alias"}
    module = redirect("legacy_test_alias", "a3_research_agent", namespace)
    assert importlib.import_module("legacy_test_alias") is module
    assert namespace["__doc__"] == module.__doc__
