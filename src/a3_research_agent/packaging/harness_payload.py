"""Export the complete trusted overlay without executing candidate source code."""

from collections.abc import Mapping
from pathlib import Path

# A reviewed source manifest, not a recursive copy of an experiment workspace.
HARNESS_MODULES = (
    "__init__.py",
    "agent.py",
    "analysis/__init__.py",
    "analysis/client.py",
    "context/__init__.py",
    "context/compaction.py",
    "context/manager.py",
    "context/spill.py",
    "memory/__init__.py",
    "memory/recall.py",
    "memory/state.py",
    "orchestration/__init__.py",
    "orchestration/assignments.py",
    "orchestration/common.py",
    "orchestration/contracts.py",
    "orchestration/store.py",
    "orchestration/waiting.py",
    "orchestration/packages.py",
    "orchestration/collection.py",
    "orchestration/dispatch.py",
)
FLAT_MODULES = {
    "agent.py": "a3_research_agent.harness.agent",
    "analyzer_client.py": "a3_research_agent.harness.analysis.client",
    "compaction.py": "a3_research_agent.harness.context.compaction",
    "context_manager.py": "a3_research_agent.harness.context.manager",
    "spill.py": "a3_research_agent.harness.context.spill",
    "recall.py": "a3_research_agent.harness.memory.recall",
    "memory_state.py": "a3_research_agent.harness.memory.state",
    "work_assignments.py": "a3_research_agent.harness.orchestration.assignments",
    "token_count.py": "a3_research_agent.token_count",
}
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
        "_compat.py": package / "_compat.py",
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
    for filename, module in FLAT_MODULES.items():
        files[filename] = (
            '"""Flat entry compatibility for an explicitly derived Harness."""\n'
            "from a3_research_agent._compat import redirect\n"
            f"_implementation = redirect(__name__, {module!r}, globals())\n"
        )
        if filename == "agent.py":
            files[filename] += '\nif __name__ == "__main__":\n    _implementation.main()\n'
    for name, content in (extension_sources or {}).items():
        if name not in EXTENSION_NAMES or not isinstance(content, str):
            raise ValueError("Only explicitly selected text Harness extensions may be attached")
        files[name] = content
    return files
