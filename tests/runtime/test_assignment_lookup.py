import json

import pytest
from test_work_assignments import Gateway, load


def test_missing_is_explicit_read_only_and_not_permission_to_retry(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    original = m.STATE.read_bytes()
    for action in ["list", "reconcile"]:
        g = Gateway()
        result = m.dispatch(g, {"action": action, "id": "missing"})
        assert result["found"] is False
        assert result["creation_status"] == "not_recorded_in_checked_ledger"
        assert result["retry_safe"] is False
        assert result["source"]["sha256"] == m.digest(original)
        assert m.STATE.read_bytes() == original
        assert not any(method == "POST" for method, _, _ in g.calls)


def test_pending_creation_is_found_not_absent(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {"x": {"status": "creation_uncertain"}}})
    result = m.dispatch(Gateway(), {"action": "list", "id": "x"})
    assert result["found"] is True
    assert result["assignment"]["status"] == "creation_uncertain"


def test_archived_assignment_is_found_and_never_recreated(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    raw = json.dumps({"id": "x", "status": "decided", "decision": "follow_up"}).encode()
    p = tmp_path / f"orchestration/archive/x-{m.digest(raw)}.json"
    p.parent.mkdir()
    p.write_bytes(raw)
    result = m.dispatch(Gateway(), {"action": "reconcile", "id": "x"})
    assert result["found"] is True and result["location"] == "archive"
    assert result["assignment"]["decision"] == "follow_up"
    assert m.load()["assignments"] == {}


@pytest.mark.parametrize("failure", ["corrupt", "symlink", "alias"])
def test_invalid_lookup_does_not_claim_absence(tmp_path, monkeypatch, failure):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    raw = b'{"id":"x","status":"decided"}'
    p = tmp_path / f"orchestration/archive/x-{m.digest(raw)}.json"
    p.parent.mkdir()
    if failure == "corrupt":
        p.write_bytes(raw + b" ")
    elif failure == "symlink":
        p.symlink_to(tmp_path / "orchestration/assignments.json")
    with pytest.raises(ValueError):
        m.dispatch(Gateway(), {"action": "list", "id": "../x" if failure == "alias" else "x"})


def test_lookup_binds_result_to_same_source_bytes_not_stale_memory(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    stale = {"version": 1, "assignments": {}}
    m.save({"version": 1, "assignments": {"x": {"status": "creation_uncertain"}}})
    result = m.lookup_assignment(stale, "x")
    assert result["found"] is True
    assert result["source"]["sha256"] == m.digest(m.STATE.read_bytes())


def test_ambiguous_archives_and_symlink_directory_do_not_report_absent(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    directory = tmp_path / "orchestration/archive"
    directory.mkdir()
    for decision in ["reject", "follow_up"]:
        raw = json.dumps({"id": "x", "status": "decided", "decision": decision}).encode()
        (directory / f"x-{m.digest(raw)}.json").write_bytes(raw)
    with pytest.raises(ValueError):
        m.dispatch(Gateway(), {"action": "list", "id": "x"})
    for p in directory.iterdir():
        p.unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        m.dispatch(Gateway(), {"action": "list", "id": "x"})


@pytest.mark.parametrize("bad", ["filename", "identity", "not_closed"])
def test_invalid_archive_not_silent_absence(tmp_path, monkeypatch, bad):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    raw = json.dumps(
        {
            "id": "other" if bad == "identity" else "x",
            "status": "assigned" if bad == "not_closed" else "decided",
        }
    ).encode()
    p = (
        tmp_path
        / "orchestration/archive"
        / ("x-not-a-digest.json" if bad == "filename" else f"x-{m.digest(raw)}.json")
    )
    p.parent.mkdir()
    p.write_bytes(raw)
    with pytest.raises(ValueError):
        m.dispatch(Gateway(), {"action": "list", "id": "x"})


def test_other_assignment_sharing_prefix_is_not_confused(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {}})
    raw = b'{"id":"x-other","status":"decided"}'
    p = tmp_path / f"orchestration/archive/x-other-{m.digest(raw)}.json"
    p.parent.mkdir()
    p.write_bytes(raw)
    assert m.dispatch(Gateway(), {"action": "list", "id": "x"})["found"] is False
