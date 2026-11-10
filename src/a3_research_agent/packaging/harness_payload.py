"""Export the complete trusted overlay without executing candidate source code."""

from collections.abc import Mapping
from pathlib import Path

# A reviewed source manifest, not a recursive copy of an experiment workspace.
HARNESS_MODULES = (
    "__init__.py",
    "agent.py",
    "skill_registry.py",
    "input_skills.py",
    "lifecycle.py",
    "waiting.py",
    "planning.py",
    "job_preflight.py",
    "analysis/__init__.py",
    "analysis/client.py",
    "context/__init__.py",
    "context/compaction.py",
    "context/manager.py",
    "context/spill.py",
    "memory/__init__.py",
    "memory/recall.py",
    "memory/state.py",
)
EXTENSION_NAMES = frozenset({"skill_tools.py", "web_tools.py"})


def build_harness_payload(extension_sources: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return source bytes as UTF-8 text; extensions are explicit caller-selected bytes.

    This exports the installed/source overlay, never a candidate workspace or its
    credentials. Applying it to a retained Harness creates a new derived version;
    callers keep its compatible extensions and evidence separately selected.
    """
    package = Path(__file__).resolve().parents[1]
    sources = {
        "__init__.py": package / "__init__.py",
        "token_count.py": package / "token_count.py",
        "packaging/__init__.py": package / "packaging/__init__.py",
        "packaging/harness_payload.py": Path(__file__).resolve(),
    }
    sources.update({"harness/" + name: package / "harness" / name for name in HARNESS_MODULES})
    files = {}
    for name, path in sources.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing regular Harness source: " + name)
        files["a3_research_agent/" + name] = path.read_bytes().decode("utf-8")
    for name, content in (extension_sources or {}).items():
        if name not in EXTENSION_NAMES or not isinstance(content, str):
            raise ValueError("Only explicitly selected text Harness extensions may be attached")
        files[name] = content
    return files
