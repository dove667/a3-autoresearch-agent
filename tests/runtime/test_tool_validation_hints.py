"""The model sees typed high-level operations and bounded corrective failures."""

import json

import pytest

from a3_research_agent.harness import agent, tool_catalog, tool_contracts, tool_results


def call(name, arguments):
    return agent.dispatch_tool_call({"name": name, "arguments": json.dumps(arguments)})


def test_invalid_json_and_nonobject_arguments_are_explicit():
    result = agent.dispatch_tool_call({"name": "read", "arguments": "not-json"})
    assert result["ok"] is False and result["error"] == "invalid_tool_json"
    assert call("read", ["file"])["error"] == "invalid_arguments"
    assert call("read", None)["error"] == "invalid_arguments"


def test_required_and_unknown_fields_are_rejected_before_execution(monkeypatch):
    monkeypatch.setattr(agent, "tool", lambda *args: pytest.fail("invalid request executed"))
    result = call("read", {})
    assert result["error"] == "missing_arguments" and "path" in result["hint"]
    assert call("gpu_status", {"path": "/control/inputs"})["error"] == "invalid_arguments"


@pytest.mark.parametrize("name", ["gateway", "preflight", "progress", "wait", "job_export"])
def test_retired_public_entrypoints_are_not_aliases(name, monkeypatch):
    monkeypatch.setattr(agent, "tool", lambda *args: pytest.fail("legacy request executed"))
    result = call(name, {})
    assert result["ok"] is False and result["error"] == "unknown_tool"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("SECRET /private/path token=xyz"),
        RuntimeError("SECRET"),
        PermissionError("SECRET"),
        FileNotFoundError("SECRET"),
    ],
)
def test_unexpected_private_exceptions_are_not_echoed(error, monkeypatch):
    def execute(*args):
        raise error

    monkeypatch.setattr(agent, "tool", execute)
    result = call("read", {"path": "example.txt"})
    assert result["ok"] is False and result["hint"]
    assert "SECRET" not in str(result) and "token=xyz" not in str(result)


def test_deliberate_validation_hint_is_preserved(monkeypatch):
    def execute(*args):
        raise tool_contracts.ToolInputError("invalid_page", "Use limit from1to8000 characters.")

    monkeypatch.setattr(agent, "tool", execute)
    result = call("read", {"path": "example.txt"})
    assert result == {
        "ok": False,
        "error": "invalid_page",
        "hint": "Use limit from1to8000 characters.",
    }


def test_real_wait_validation_returns_actionable_error_without_transport(monkeypatch):
    monkeypatch.setattr(
        agent, "req", lambda *args, **kwargs: pytest.fail("invalid wait made network call")
    )
    result = call("job_wait", {"job_ids": ["not-a-job"]})
    assert result["ok"] is False and result["error"] == "invalid_job_ids"
    assert "32" in result["hint"]


def test_registered_schemas_are_unique_closed_and_current():
    assert len(tool_catalog.BY_NAME) == len(tool_catalog.TOOLS)
    assert all(tool["parameters"]["additionalProperties"] is False for tool in tool_catalog.TOOLS)
    properties = {tool["name"]: tool["parameters"]["properties"] for tool in tool_catalog.TOOLS}
    assert properties["job_wait"]["timeout_seconds"]["maximum"] == 600
    assert properties["idle_wait"]["seconds"]["maximum"] == 600
    assert "goal" not in properties["plan"]
    assert "attachments" in properties["job_submit"]
    assert (
        "files" not in properties["job_submit"]
        and "workspace_files" not in properties["job_submit"]
    )
    assert "CURRENT run only" in tool_catalog.BY_NAME["recall"]["description"]
    assert not {"method", "body"} & properties["runtime_status"].keys()


def test_success_and_failure_results_have_same_top_level_contract():
    assert tool_results.normalize({"status": "completed"}) == {"ok": True, "status": "completed"}
    assert tool_results.normalize(["observation"])["items"] == ["observation"]
    result = tool_results.normalize({"error": "source_changed"})
    assert result["ok"] is False and result["error"] == "source_changed" and result["hint"]
    assert tool_results.normalize("denied")["error"] == "invalid_tool_response"


def test_http_private_text_does_not_become_a_model_hint():
    result = tool_results.normalize(
        {
            "http_error": 500,
            "body": json.dumps(
                {"detail": {"message": "SECRET token=xyz", "traceback": "SECRET /private/path"}}
            ),
        }
    )
    assert result["ok"] is False and result["http_status"] == 500
    assert "SECRET" not in str(result) and "token=xyz" not in str(result)


def test_observation_failure_cannot_hide_an_executed_host_result(monkeypatch):
    returned = {"job_id": "a" * 32, "status": "queued"}
    monkeypatch.setattr(agent, "req", lambda *args, **kwargs: returned)

    def fail_receipt(*args):
        raise OSError("private storage failure")

    monkeypatch.setattr(agent.operational_state, "record_gateway", fail_receipt)
    monkeypatch.setattr(agent, "log", fail_receipt)
    assert agent.request_host("POST", "/jobs", {"command": ["true"]}) is returned


def test_input_header_names_the_canonical_manifest_path(tmp_path):
    from input_source_support import deployment_source

    from a3_research_agent.host.inputs.versions import InputVersions

    versions = InputVersions(deployment_source(tmp_path / "source"), tmp_path / "run")
    versions.activate(versions.prepare(), "start")
    assert "A3_INPUT_ROOT 下 input-manifest.json" in versions.model_instructions()[1]


def test_submission_unknown_keeps_reconciliation_ids_without_raw_body():
    result = tool_results.normalize(
        {
            "http_error": 502,
            "status": "submission_unknown",
            "job_id": "a" * 32,
            "prepared_id": "prepared-" + "b" * 32,
            "body": json.dumps({"detail": {"job_id": "a" * 32, "message": "SECRET ssh error"}}),
        }
    )
    assert result["ok"] is False and result["error"] == "submission_unknown"
    assert result["status"] == "submission_unknown" and result["job_id"] == "a" * 32
    assert result["prepared_id"] == "prepared-" + "b" * 32
    assert "body" not in result and "SECRET" not in str(result)
    assert "job_status" in result["hint"]


def test_wait_observation_failure_is_not_a_successful_observation():
    result = tool_results.normalize({"reason": "observation_error", "path": "/jobs/" + "a" * 32})
    assert result["ok"] is False and result["error"] == "wait_observation_failed"
    assert result["hint"]


def test_http_validation_error_keeps_code_without_echoing_private_detail():
    result = tool_results.normalize(
        {
            "http_error": 422,
            "body": json.dumps(
                {"detail": {"error": "invalid_asset", "hint": "SECRET", "path": "/private/SECRET"}}
            ),
        }
    )
    assert result["error"] == "invalid_asset" and result["http_status"] == 422
    assert "SECRET" not in str(result)


def test_empty_recall_query_is_a_structured_validation_failure():
    result = call("recall", {"keywords": []})
    assert result["ok"] is False and result["error"] and result["hint"]
