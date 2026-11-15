"""Native deployments use explicit Host workspace and reference paths."""

import json


def test_native_roots_are_explicit_host_paths(tmp_path, monkeypatch):
    from a3_research_agent.harness import paths

    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path / "reference"))
    monkeypatch.setenv("A3_MEMORY_ROOT", str(tmp_path / "memory"))
    assert paths.workspace_root() == tmp_path / "work"
    assert paths.input_root() == tmp_path / "inputs"
    assert paths.reference_root() == tmp_path / "reference"
    assert paths.memory_root() == tmp_path / "memory"


def test_native_read_and_edit_use_host_workspace_paths(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path.parent / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path.parent / "reference"))
    (tmp_path / "fact.txt").write_text("old value")
    read = agent.dispatch_tool_call(
        {"name": "read", "arguments": json.dumps({"path": str(tmp_path / "fact.txt")})}
    )
    assert read["content"] == "old value"
    result = agent.tool(
        "edit", {"path": str(tmp_path / "fact.txt"), "old_text": "old", "new_text": "new"}
    )
    assert result["status"] == "edited" and (tmp_path / "fact.txt").read_text() == "new value"


def test_default_wait_manifest_uses_native_input_directory(tmp_path, monkeypatch):
    from a3_research_agent.harness import waiting

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path))
    (tmp_path / "input-manifest.json").write_text('{"status":"updating"}')
    result = waiting.wait_for_change(lambda *args, **kwargs: {}, seconds=1)
    assert result["reason"] == "inputs_changed"
