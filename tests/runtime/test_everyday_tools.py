"""Common research operations do not require hand-built HTTP or file hashes."""

import hashlib
import json

import pytest


@pytest.fixture(autouse=True)
def native_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path))
    for name, variable in (("inputs", "A3_INPUT_ROOT"), ("reference", "A3_REFERENCE_ROOT")):
        directory = tmp_path.parent / f"{name}-{tmp_path.name}"
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))


def test_invalid_command_is_explained_before_any_submission(tmp_path):
    from a3_research_agent.harness import job_tools

    calls = []
    result = job_tools.dispatch(
        "job_submit", {"command": "python train.py"}, lambda *a: calls.append(a), workspace=tmp_path
    )
    assert result["error"] == "invalid_command"
    assert "array" in result["hint"]
    assert not calls


def test_uncertain_submission_is_returned_once_without_retry(tmp_path):
    from a3_research_agent.harness import job_tools

    calls = []
    response = {
        "http_error": 502,
        "body": json.dumps(
            {
                "detail": {
                    "code": "submission_unknown",
                    "job_id": "b" * 32,
                    "status_path": "/jobs/" + "b" * 32,
                }
            }
        ),
    }

    def request(*args):
        calls.append(args)
        return response

    result = job_tools.dispatch(
        "job_submit", {"command": ["python", "-c", "print(1)"]}, request, workspace=tmp_path
    )
    assert len(calls) == 1
    assert result["job_id"] == "b" * 32
    assert result["status"] == "submission_unknown"
    assert result["status_path"] == "/jobs/" + "b" * 32


def test_job_artifact_decodes_bounded_text_and_preserves_source_identity():
    from a3_research_agent.harness import job_tools

    raw = "结果\nabcdefgh".encode()
    calls = []

    def request(*args):
        calls.append(args)
        return {
            "path": "result.json",
            "encoding": "utf-8",
            "content": "abcd",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "unit": "characters",
            "offset": 3,
            "end": 7,
            "total": 11,
            "eof": False,
        }

    result = job_tools.dispatch(
        "job_artifact",
        {
            "job_id": "c" * 32,
            "path": "result.json",
            "area": "outputs",
            "offset": 3,
            "limit": 4,
        },
        request,
    )
    assert result["content"] == "abcd"
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["job_id"] == "c" * 32 and result["area"] == "outputs"
    assert "area=outputs" in calls[0][1]
    assert "offset=3" in calls[0][1] and "limit=4" in calls[0][1]
    assert result["eof"] is False


def test_job_artifact_requires_paged_host_response():
    from a3_research_agent.harness import job_tools

    result = job_tools.dispatch(
        "job_artifact",
        {"job_id": "c" * 32, "path": "result.json", "area": "outputs"},
        lambda *args: {"encoding": "base64", "data": "YWJj"},
    )
    assert result["error"] == "artifact_pagination_unavailable"
    assert "deployment" in result["hint"]


def test_workspace_search_finds_lines_and_skips_symlink_escape(tmp_path):
    from a3_research_agent.harness import workspace_tools

    (tmp_path / "a.py").write_text("first\nmodel.forward(inputs)\nlast\n")
    (tmp_path / "escape").symlink_to(tmp_path.parent)
    result = workspace_tools.dispatch(
        "search_files", {"path": ".", "query": "FORWARD"}, workspace=tmp_path, readonly_roots=()
    )
    assert [(m["path"], m["line"]) for m in result["matches"]] == [("a.py", 2)]
    assert "model.forward(inputs)" in result["matches"][0]["text"]
    assert (
        result["matches"][0]["sha256"]
        == hashlib.sha256((tmp_path / "a.py").read_bytes()).hexdigest()
    )


def test_workspace_edit_requires_unique_match_and_preserves_file_on_conflict(tmp_path):
    from a3_research_agent.harness import workspace_tools

    path = tmp_path / "train.py"
    path.write_text("x=1\nx=1\n")
    result = workspace_tools.dispatch(
        "edit",
        {"path": "train.py", "old_text": "x=1", "new_text": "x=2"},
        workspace=tmp_path,
        readonly_roots=(),
    )
    assert result["error"] == "ambiguous_edit" and path.read_text() == "x=1\nx=1\n"
    result = workspace_tools.dispatch(
        "edit",
        {"path": "train.py", "old_text": "x=1", "new_text": "x=2", "replace_all": True},
        workspace=tmp_path,
        readonly_roots=(),
    )
    assert result["replacements"] == 2 and path.read_text() == "x=2\nx=2\n"


def test_workspace_edit_honors_expected_hash_and_read_only_roots(tmp_path):
    from a3_research_agent.harness import workspace_tools

    path = tmp_path / "train.py"
    path.write_text("x=1\n")
    result = workspace_tools.dispatch(
        "edit",
        {"path": "train.py", "old_text": "x=1", "new_text": "x=2", "expected_sha256": "0" * 64},
        workspace=tmp_path,
    )
    assert result["error"] == "revision_conflict" and path.read_text() == "x=1\n"


def test_builtin_web_tools_use_fixed_host_routes():
    from a3_research_agent.harness import research_web

    calls = []
    result = research_web.dispatch(
        "web_search",
        {"query": "ACT policy", "max_results": 3},
        lambda *args: calls.append(args) or {"results": []},
    )
    assert result == {"results": []}
    assert calls == [("POST", "/web/search", {"query": "ACT policy", "max_results": 3})]


def test_agent_exposes_builtin_tools_without_optional_extensions():
    from a3_research_agent.harness import agent

    names = [tool["name"] for tool in agent.tools]
    assert {
        "job_submit",
        "job_status",
        "job_list",
        "job_artifact",
        "search_files",
        "edit",
        "web_search",
        "web_fetch",
        "web_status",
    } <= set(names)
    assert {"gateway", "preflight", "progress", "wait", "job_export"}.isdisjoint(names)
    assert {
        "job_prepare",
        "job_wait",
        "idle_wait",
        "research_status",
        "export_create",
        "vision_inspect",
    } <= set(names)
    assert len(names) == len(set(names))


def test_malformed_tool_arguments_return_actionable_json_error():
    from a3_research_agent.harness.agent import dispatch_tool_call

    result = dispatch_tool_call({"name": "write", "arguments": '{"path": "secret'})
    assert result["error"] == "invalid_tool_json"
    assert "secret" not in str(result)
    assert dispatch_tool_call({"name": "read", "arguments": "[]"})["error"] == "invalid_arguments"


def test_reading_directory_explains_the_required_tool(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    result = agent.dispatch_tool_call(
        {"name": "read", "arguments": json.dumps({"path": str(agent.runtime_paths.input_root())})}
    )
    assert result["error"] == "path_is_directory" and "list" in result["hint"]


def test_read_page_bounds_explain_invalid_limit_without_exposing_source():
    from a3_research_agent.harness import agent

    (agent.runtime_paths.input_root() / "execution.md").write_text("private source body")
    result = agent.dispatch_tool_call(
        {
            "name": "read",
            "arguments": json.dumps(
                {"path": str(agent.runtime_paths.input_root() / "execution.md"), "limit": 9500}
            ),
        }
    )
    assert result["error"] == "invalid_limit" and "8000" in result["hint"]


def test_json_field_selection_explains_non_object_sources(tmp_path):
    from a3_research_agent.harness import file_tools

    for raw in (b"not JSON", b'["one", "two"]'):
        path = tmp_path / "report.json"
        path.write_bytes(raw)
        result = file_tools.dispatch(
            "read", {"path": str(path), "fields": ["score"]}, workspace=tmp_path, readonly_roots=()
        )
        assert result["error"] in {"source_not_json", "json_root_not_object"}
        assert "page" in result["hint"].lower()


def test_workspace_search_reports_truncation_and_edit_respects_read_only_input(tmp_path):
    from a3_research_agent.harness import workspace_tools

    (tmp_path / "many.txt").write_text("match\nmatch\nmatch\n")
    result = workspace_tools.dispatch(
        "search_files", {"query": "match", "max_matches": 1}, workspace=tmp_path
    )
    assert result["truncated"] is True and len(result["matches"]) == 1
    result = workspace_tools.dispatch(
        "edit",
        {
            "path": str(workspace_tools.paths.input_root() / "goal.md"),
            "old_text": "old",
            "new_text": "new",
        },
        workspace=tmp_path,
    )
    assert result["error"] == "path_not_accessible"


def test_workspace_edit_cannot_replace_managed_plan(tmp_path):
    from a3_research_agent.harness import workspace_tools

    path = tmp_path / "plan.json"
    path.write_text("old")
    result = workspace_tools.dispatch(
        "edit",
        {"path": str(path), "old_text": "old", "new_text": "new"},
        workspace=tmp_path,
        protected=[path],
    )
    assert result["error"] == "managed_state_read_only" and path.read_text() == "old"
