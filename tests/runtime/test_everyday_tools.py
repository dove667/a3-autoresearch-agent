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


def test_job_submit_hashes_workspace_sources_and_uses_canonical_destinations(tmp_path):
    from a3_research_agent.harness import job_tools

    source = tmp_path / "research/train.py"
    source.parent.mkdir()
    source.write_text("print('train')\n")
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"job_id": "a" * 32, "status": "queued"}

    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "train.py"],
            "gpu": True,
            "workspace_files": [{"path": str(source), "destination": "train.py"}],
        },
        request,
        workspace=tmp_path,
    )
    assert result["job_id"] == "a" * 32
    assert calls == [
        (
            "POST",
            "/jobs",
            {
                "command": ["python", "train.py"],
                "gpu": True,
                "workspace_files": [
                    {
                        "path": "research/train.py",
                        "destination": "train.py",
                        "sha256": hashlib.sha256(b"print('train')\n").hexdigest(),
                    }
                ],
            },
        )
    ]


def test_invalid_command_is_explained_before_any_submission(tmp_path):
    from a3_research_agent.harness import job_tools

    calls = []
    result = job_tools.dispatch(
        "job_submit", {"command": "python train.py"}, lambda *a: calls.append(a), workspace=tmp_path
    )
    assert result["error"] == "invalid_command"
    assert "array" in result["hint"]
    assert not calls


def test_source_escape_and_unexpected_attachment_fields_do_not_submit(tmp_path):
    from a3_research_agent.harness import job_tools

    for reference in [{"path": "../private.py"}, {"path": "train.py", "bytes": 12}]:
        calls = []
        result = job_tools.dispatch(
            "job_submit",
            {"command": ["python", "train.py"], "workspace_files": [reference]},
            lambda *a, calls=calls: calls.append(a),
            workspace=tmp_path,
        )
        assert result.get("error") and not calls


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
        "job_submit", {"command": ["python", "train.py"]}, request, workspace=tmp_path
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
    assert len(names) == len(set(names))


def test_malformed_tool_arguments_return_actionable_json_error():
    from a3_research_agent.harness.agent import dispatch_tool_call

    result = dispatch_tool_call({"name": "write", "arguments": '{"path": "secret'})
    assert result["error"] == "invalid_tool_json"
    assert "secret" not in str(result)
    assert dispatch_tool_call({"name": "read", "arguments": "[]"})["error"] == "invalid_arguments"


def test_reading_directory_explains_the_required_tool(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    monkeypatch.setattr(agent.pathlib.Path, "is_dir", lambda self: True)
    result = agent.dispatch_tool_call(
        {"name": "read", "arguments": json.dumps({"path": str(agent.runtime_paths.input_root())})}
    )
    assert result["error"] == "path_is_directory" and "list" in result["hint"]


def test_read_page_bounds_explain_invalid_limit_without_exposing_source(monkeypatch):
    from a3_research_agent.harness import agent

    monkeypatch.setattr(agent.pathlib.Path, "is_dir", lambda self: False)
    result = agent.dispatch_tool_call(
        {
            "name": "read",
            "arguments": json.dumps(
                {"path": str(agent.runtime_paths.input_root() / "execution.md"), "limit": 9500}
            ),
        }
    )
    assert result["error"] == "invalid_limit" and "8000" in result["hint"]


def test_json_field_selection_explains_non_object_sources():
    from a3_research_agent.harness.agent import select_json_fields

    for raw in (b"not JSON", b'["one", "two"]'):
        result = select_json_fields(raw, ["score"], "report.json")
        assert result["error"] in {"source_not_json", "json_root_not_object"}
        assert "read" in result["hint"]


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

    path = tmp_path / "conversation/research-plan.json"
    path.parent.mkdir()
    path.write_text("old")
    result = workspace_tools.dispatch(
        "edit",
        {"path": str(path), "old_text": "old", "new_text": "new"},
        workspace=tmp_path,
        protected=[path],
    )
    assert result["error"] == "managed_state_read_only" and path.read_text() == "old"


def test_conflicting_inline_and_attached_files_are_rejected_before_submission(tmp_path):
    from a3_research_agent.harness import job_tools

    (tmp_path / "research").mkdir()
    (tmp_path / "research/train.py").write_text("print('source')")
    calls = []
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "train.py"],
            "workspace_files": [{"path": "research/train.py", "destination": "train.py"}],
            "files": {"train.py": "print('inline')"},
        },
        lambda *args: calls.append(args),
        workspace=tmp_path,
    )
    assert result["error"] == "conflicting_job_files" and not calls


def test_preflight_hashes_sources_and_submits_production_only_once(tmp_path, monkeypatch):
    import base64

    from a3_research_agent.harness import agent

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "research/train.py"
    source.parent.mkdir()
    source.write_text("print('train')\n")
    submissions = []
    job = {
        "command": ["python", "research/train.py"],
        "gpu": True,
        "workspace_files": [{"path": "research/train.py"}],
    }

    def request(method, path, body=None):
        if path == "/sessions/self":
            return {"is_main": True}
        if method == "POST" and path == "/jobs":
            submissions.append(body)
            return {"job_id": ("a" if len(submissions) == 1 else "b") * 32, "status": "queued"}
        if "/artifacts?" in path:
            raw = json.dumps({"schema": 1, "status": "passed"}).encode()
            return {"encoding": "base64", "data": base64.b64encode(raw).decode()}
        return {"job_id": "a" * 32, "status": "completed", "exit_code": 0}

    monkeypatch.setattr(agent, "req", request)
    checked = agent.tool("preflight", {"action": "check", "job": job})
    assert checked["job_id"] == "a" * 32
    assert submissions[0]["workspace_files"] == [
        {
            "path": "research/train.py",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    arguments = {"action": "submit", "job": job, "preflight_id": "a" * 32}
    first, repeated = agent.tool("preflight", arguments), agent.tool("preflight", arguments)
    assert first == repeated == {"job_id": "b" * 32, "status": "queued"}
    assert len(submissions) == 2
    assert submissions[1]["command"] == ["python", "research/train.py"]


def test_preflight_checks_old_explicit_source_hash_before_submission(tmp_path):
    from a3_research_agent.harness import job_tools, tool_contracts

    (tmp_path / "research").mkdir()
    (tmp_path / "research/train.py").write_text("new contents")
    with pytest.raises(tool_contracts.ToolInputError) as caught:
        job_tools.submit_request(
            {
                "command": ["python", "research/train.py"],
                "gpu": True,
                "workspace_files": [{"path": "research/train.py", "sha256": "0" * 64}],
            },
            tmp_path,
            allow_pinned_sources=True,
        )
    assert caught.value.code == "source_revision_conflict"


def test_job_source_limits_match_host_attachment_contract(tmp_path):
    from a3_research_agent.harness import job_tools

    research = tmp_path / "research"
    research.mkdir()
    large = research / "large.py"
    large.write_text("x" * (1024 * 1024 + 1))
    calls = []
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "research/large.py"],
            "workspace_files": [{"path": "research/large.py"}],
        },
        lambda *args: calls.append(args),
        workspace=tmp_path,
    )
    assert result["error"] == "source_file_too_large" and not calls
    for index in range(9):
        (research / f"small-{index}.py").write_text("x" * 1024 * 1024)
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "research/small-0.py"],
            "workspace_files": [{"path": f"research/small-{index}.py"} for index in range(9)],
        },
        lambda *args: calls.append(args),
        workspace=tmp_path,
    )
    assert result["error"] == "source_attachments_too_large" and not calls


def test_job_source_requires_host_supported_research_path(tmp_path):
    from a3_research_agent.harness import job_tools

    (tmp_path / "train.py").write_text("print('train')")
    calls = []
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "train.py"],
            "workspace_files": [{"path": "train.py"}],
        },
        lambda *args: calls.append(args),
        workspace=tmp_path,
    )
    assert result["error"] == "invalid_source_path" and not calls
