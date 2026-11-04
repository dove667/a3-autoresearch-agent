import json

from test_work_assignments import Gateway, load


def test_default_list_is_bounded_and_preserves_full_ledger(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    items = {
        str(i): {
            "id": str(i),
            "role": "harness",
            "status": "decided",
            "decision": "follow_up",
            "reason": "evidence " * 500,
        }
        for i in range(100)
    }
    items["live"] = {"id": "live", "role": "policy", "status": "assigned", "session_id": "worker-1"}
    m.save({"version": 1, "assignments": items})
    before = m.STATE.read_bytes()
    result = m.dispatch(Gateway(), {"action": "list"})
    assert len(json.dumps(result).encode()) < 6000
    assert result["omitted_assignments"] > 0
    assert any(x["id"] == "live" for x in result["assignments"])
    assert m.STATE.read_bytes() == before
    detail = m.dispatch(Gateway(), {"action": "list", "id": "0"})
    assert detail["assignment"]["reason"] == items["0"]["reason"]
    assert detail["source"]["sha256"]


def test_empty_ledger_remains_valid_without_creating_state(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    result = m.dispatch(Gateway(), {"action": "list"})
    assert result["assignments"] == []
    assert result["source"]["sha256"] is None
    assert not m.STATE.exists()


def test_many_pending_tasks_are_explicitly_omitted_not_lost(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save(
        {
            "version": 1,
            "assignments": {
                str(i): {"id": str(i), "role": "policy", "status": "assigned"} for i in range(30)
            },
        }
    )
    result = m.dispatch(Gateway(), {"action": "list"})
    assert result["omitted_pending"] == 18
    assert result["omitted_assignments"] == 18
    assert (
        m.dispatch(Gateway(), {"action": "list", "id": "29"})["assignment"]["status"] == "assigned"
    )
