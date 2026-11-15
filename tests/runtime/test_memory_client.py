"""Memory tools use only the Host-owned loopback service."""

from a3_research_agent.harness.memory import client


def test_unconfigured_service_has_explicit_error(monkeypatch):
    monkeypatch.delenv("A3_KNOWLEDGE_SERVICE_URL", raising=False)
    monkeypatch.delenv("A3_KNOWLEDGE_SERVICE_TOKEN", raising=False)
    assert (
        client.dispatch({"action": "search", "query": "past failures"})["error"]
        == "knowledge_service_unavailable"
    )


def test_nonlocal_service_is_rejected_without_network(monkeypatch):
    monkeypatch.setenv("A3_KNOWLEDGE_SERVICE_URL", "http://example.com")
    monkeypatch.setenv("A3_KNOWLEDGE_SERVICE_TOKEN", "private")
    assert client.call({"action": "list"})["error"] == "knowledge_service_unavailable"


def test_research_focus_changes_refresh_suggestions_and_writes_invalidate(monkeypatch):
    client._CONTEXT_CACHE.clear()
    calls = []

    def request(args):
        calls.append(args)
        return {"items": [{"id": "lesson"}]}

    monkeypatch.setattr(client, "call", request)
    plan = {
        "revision": 1,
        "direction": {"objective": "Improve placement"},
        "focus": ["a"],
        "steps": {"a": {"task": "Evaluate checkpoint"}},
    }
    client.suggestions(plan)
    client.suggestions(plan)
    assert len(calls) == 1
    assert "Evaluate checkpoint" in calls[0]["query"]
    client.dispatch({"action": "write", "content": "New finding"})
    client.suggestions(plan)
    assert len(calls) == 3
