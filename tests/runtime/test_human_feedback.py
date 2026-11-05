import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import HumanFeedback


def register(tmp_path, text="Human repaired the runtime; check the interrupted collection job."):
    source = tmp_path / "human-feedback.md"
    source.write_text(text)
    feedback = HumanFeedback(tmp_path / "run")
    feedback.register(source, "main")
    return feedback, source


def client(tmp_path, feedback, requests):
    return TestClient(
        create_app(
            model_name="fixture",
            sessions={"main-token": "main", "worker-token": "worker"},
            forward_model=lambda **request: requests.append(deepcopy(request)) or {"output": []},
            remote_call=lambda *args: pytest.fail("No remote calls"),
            usage_log=tmp_path / "usage.jsonl",
            human_feedback=feedback,
        )
    )


def test_feedback_arrives_after_complete_history_and_persists_at_same_boundary(tmp_path):
    feedback, source = register(tmp_path)
    requests = []
    api = client(tmp_path, feedback, requests)
    original = [
        {"role": "user", "content": "original task"},
        {"type": "function_call", "call_id": "call-one", "name": "status", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-one", "output": "running"},
    ]
    saved = deepcopy(original)
    headers = {"Authorization": "Bearer main-token"}
    assert api.post("/v1/responses", headers=headers, json={"input": original}).status_code == 200
    assert requests[0]["input"][:-1] == saved
    injected = requests[0]["input"][-1]
    assert injected["role"] == "user" and source.read_text() in injected["content"]
    following = original + [{"role": "assistant", "content": "I will reconcile the stopped job."}]
    assert api.post("/v1/responses", headers=headers, json={"input": following}).status_code == 200
    assert requests[1]["input"] == original + [injected] + following[-1:]
    assert original == saved
    events = [json.loads(x) for x in (feedback.directory / "events.jsonl").read_text().splitlines()]
    assert [x["event"] for x in events] == ["registered", "anchored", "forwarded"]
    usage = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert all(x["human_feedback_ids"] == ["feedback-0001"] for x in usage)


def test_feedback_survives_host_restart_and_overwritten_source_does_not_autoload(tmp_path):
    feedback, source = register(tmp_path)
    original = [{"role": "user", "content": "original"}]
    first, _ = feedback.apply("main", {"input": original})
    source.write_text("A new expert suggestion")
    restored = HumanFeedback(tmp_path / "run")
    following = original + [{"role": "assistant", "content": "next"}]
    second, ids = restored.apply("main", {"input": following})
    assert second["input"][1] == first["input"][1]
    assert ids == ["feedback-0001"]
    restored.register(source, "main")
    assert restored.register(source, "main")["new"] is False
    third, ids = restored.apply("main", {"input": following})
    assert ids == ["feedback-0001", "feedback-0002"]
    assert third["input"][-1]["content"].endswith("A new expert suggestion")
    assert len([x for x in third["input"] if x.get("role") == "user"]) == 3
    assert (restored.directory / "feedback-0001.md").read_text() != source.read_text()


def test_unconfigured_and_other_identity_requests_are_unchanged(tmp_path):
    empty = HumanFeedback(tmp_path / "empty-run")
    arguments = {"input": [{"role": "user", "content": "task"}]}
    assert empty.apply("main", arguments) == (arguments, [])
    assert not empty.directory.exists()
    feedback, _ = register(tmp_path)
    assert feedback.apply("worker", arguments) == (arguments, [])
    requests = []
    api = client(tmp_path, feedback, requests)
    assert (
        api.post(
            "/v1/responses", headers={"Authorization": "Bearer worker-token"}, json=arguments
        ).status_code
        == 200
    )
    assert requests[0]["input"] == arguments["input"]


def test_reanchoring_uses_end_of_complete_new_history_and_existing_feedback_is_not_duplicated(
    tmp_path,
):
    feedback, _ = register(tmp_path)
    first, _ = feedback.apply("main", {"input": [{"role": "user", "content": "old history"}]})
    compacted = [{"role": "user", "content": "compressed history"}]
    new, _ = feedback.apply("main", {"input": compacted})
    assert new["input"][0] == compacted[0]
    assert new["input"][-1] == first["input"][-1]
    again, _ = feedback.apply("main", new)
    assert again == new
    events = [json.loads(x) for x in (feedback.directory / "events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "reanchored"


def test_feedback_rejects_incomplete_tool_pair_and_server_side_history(tmp_path):
    feedback, _ = register(tmp_path)
    with pytest.raises(ValueError, match="completed tool results"):
        feedback.apply("main", {"input": [{"type": "function_call", "call_id": "pending"}]})
    with pytest.raises(ValueError, match="full message-history"):
        feedback.apply("main", {"input": [], "previous_response_id": "previous"})


@pytest.mark.parametrize("content", [b"", b"x" * 65537, b"\xff"])
def test_feedback_rejects_empty_oversize_or_non_utf8(tmp_path, content):
    source = tmp_path / "human-feedback.md"
    source.write_bytes(content)
    with pytest.raises((ValueError, UnicodeError)):
        HumanFeedback(tmp_path / "run").register(source, "main")


def test_failed_provider_response_does_not_mark_feedback_forwarded(tmp_path):
    feedback, _ = register(tmp_path)

    def unavailable(**request):
        raise RuntimeError("controlled provider failure")

    api = TestClient(
        create_app(
            model_name="fixture",
            sessions={"main-token": "main"},
            forward_model=unavailable,
            remote_call=lambda *args: pytest.fail("No remote calls"),
            usage_log=tmp_path / "usage.jsonl",
            human_feedback=feedback,
        )
    )
    response = api.post(
        "/v1/responses",
        headers={"Authorization": "Bearer main-token"},
        json={"input": [{"role": "user", "content": "task"}]},
    )
    assert response.status_code == 502
    assert "forwarded_at" not in HumanFeedback(tmp_path / "run").records[0]
    events = [json.loads(x) for x in (feedback.directory / "events.jsonl").read_text().splitlines()]
    assert [x["event"] for x in events] == ["registered", "anchored"]


def test_feedback_cli_rejects_fresh_start_before_deployment_calls(tmp_path, monkeypatch):
    import sys

    from a3_research_agent.host import bootstrap as launch

    monkeypatch.setattr(sys, "argv", ["launch", "--feedback-file", str(tmp_path / "feedback.md")])
    monkeypatch.setattr(launch, "load_dotenv", lambda *args, **kwargs: pytest.fail("No deployment"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2
