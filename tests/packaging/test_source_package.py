"""Exercise canonical source imports without importing operational entry points."""

import importlib
from pathlib import Path


def test_canonical_package_is_importable_from_src():
    package = importlib.import_module("a3_research_agent")
    assert Path(package.__file__).resolve().parent == (
        Path(__file__).resolve().parents[2] / "src/a3_research_agent"
    )
