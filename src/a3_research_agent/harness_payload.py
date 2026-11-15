"""Export the complete maintained Harness source package."""

from pathlib import Path

# A reviewed source manifest, not a recursive copy of an experiment workspace.
HARNESS_MODULES = (
    "__init__.py",
    "agent.py",
    "paths.py",
    "state_paths.py",
    "research_status.py",
    "skill_registry.py",
    "skill_search.py",
    "semantic_index.py",
    "knowledge_client.py",
    "skill_library.py",
    "lifecycle.py",
    "waiting.py",
    "plan/__init__.py",
    "plan/store.py",
    "plan/model.py",
    "job_preparation.py",
    "control_tools.py",
    "file_tools.py",
    "host_tools.py",
    "tool_catalog.py",
    "tool_results.py",
    "job_tools.py",
    "workspace_tools.py",
    "research_web.py",
    "tool_contracts.py",
    "context/__init__.py",
    "context/compaction.py",
    "context/manager.py",
    "context/spill.py",
    "history.py",
    "memory/__init__.py",
    "memory/client.py",
    "memory/store.py",
    "memory/retrieval.py",
    "memory/sources.py",
    "jsonl_store.py",
    "operational_state.py",
)


def build_harness_payload() -> dict[str, str]:
    """Return the fixed package sources as UTF-8 text."""
    package = Path(__file__).resolve().parent
    sources = {
        "__init__.py": package / "__init__.py",
        "token_count.py": package / "token_count.py",
        "harness_payload.py": Path(__file__).resolve(),
    }
    sources.update({"harness/" + name: package / "harness" / name for name in HARNESS_MODULES})
    files = {}
    for name, path in sources.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing regular Harness source: " + name)
        files["a3_research_agent/" + name] = path.read_bytes().decode("utf-8")
    return files
