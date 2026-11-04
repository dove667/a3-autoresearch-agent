"""Supplemental collection stays data-only and within an existing worker subtree."""

import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_work_assignments import load

PRODUCER = "candidate-" + "a" * 32
CHILD = "candidate-" + "b" * 32
OTHER = "candidate-" + "c" * 32


class ArtifactGateway:
    def __init__(self):
        self.calls = []
        self.sessions = [
            {"session_id": PRODUCER, "parent": "main"},
            {"session_id": CHILD, "parent": PRODUCER},
            {"session_id": OTHER, "parent": "main"},
        ]
        self.data = {
            "research/module.py": b"# candidate data only\n",
            "research/package.json": b'{"ok":true}\n',
        }
        self.status = "exited"
        self.denied = False

    def __call__(self, method, path, data=None):
        self.calls.append((method, path))
        if path == "/sessions/self":
            return {"session_id": "main"}
        if path == "/evolution":
            return {"main_owner": "main"}
        if path == "/sessions":
            return {"sessions": self.sessions}
        if path == "/sessions/" + CHILD:
            return {"http_error": 403} if self.denied else {"status": self.status}
        if "/artifacts?" in path:
            if self.denied:
                return {"http_error": 403}
            name = parse_qs(urlsplit(path).query)["path"][0]
            return {"data": base64.b64encode(self.data[name]).decode()}
        raise AssertionError((method, path))


def fixture(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    item = {
        "id": "prep",
        "status": "decided",
        "session_id": PRODUCER,
        "evidence_source_session": PRODUCER,
        "decision": "follow_up",
        "evidence_hashes": {},
        "evidence_paths": {},
        "result": {"status": "blocked"},
    }
    m.save({"version": 1, "assignments": {"prep": item}})
    g = ArtifactGateway()
    request = {
        "action": "collect_artifacts",
        "id": "prep",
        "source_session_id": CHILD,
        "artifacts": [{"path": k, "sha256": m.digest(v)} for k, v in g.data.items()],
    }
    return m, g, request, copy.deepcopy(item)


def test_collects_exact_child_bytes_without_reopening_decision(tmp_path, monkeypatch):
    m, g, request, original = fixture(tmp_path, monkeypatch)
    result = m.dispatch(g, request)
    assert result["source_session"] == CHILD
    assert result["generation_proven"] is False
    state = m.load()
    item = state["assignments"]["prep"]
    assert {k: item[k] for k in original} == original
    for source, target in result["evidence_paths"].items():
        assert (tmp_path / target).read_bytes() == g.data[source]
        assert m.collected_bytes(state, target, m.digest(g.data[source])) == (g.data[source], CHILD)
    assert len(item["artifact_collections"]) == 1
    m.dispatch(g, request)
    assert len(m.load()["assignments"]["prep"]["artifact_collections"]) == 1
    assert all(method == "GET" for method, _ in g.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "sibling",
        "producer_self",
        "unknown",
        "cycle",
        "running",
        "denied",
        "hash",
        "private",
        "alias",
        "duplicate",
        "empty",
        "count",
        "oversize",
        "binary",
        "symlink",
        "conflict",
    ],
)
def test_refuses_invalid_collection_without_receipt(tmp_path, monkeypatch, failure):
    m, g, request, original = fixture(tmp_path, monkeypatch)
    if failure == "sibling":
        request["source_session_id"] = OTHER
    elif failure == "producer_self":
        request["source_session_id"] = PRODUCER
    elif failure == "unknown":
        request["source_session_id"] = "candidate-" + "d" * 32
    elif failure == "cycle":
        g.sessions[1]["parent"] = CHILD
    elif failure == "running":
        g.status = "running"
    elif failure == "denied":
        g.denied = True
    elif failure == "hash":
        request["artifacts"][-1]["sha256"] = "0" * 64
    elif failure == "private":
        request["artifacts"][0]["path"] = "checkpoint.json"
    elif failure == "alias":
        request["artifacts"][0]["path"] = "research/../checkpoint.json"
    elif failure == "duplicate":
        request["artifacts"].append(request["artifacts"][0])
    elif failure == "empty":
        request["artifacts"] = []
    elif failure == "count":
        request["artifacts"] *= 9
    elif failure == "oversize":
        g.data["research/module.py"] = b"x" * (1024 * 1024 + 1)
    elif failure == "binary":
        g.data["research/module.py"] = b"\xff"
        request["artifacts"][0]["sha256"] = m.digest(b"\xff")
    else:
        root = tmp_path / "orchestration/results/prep/child-artifacts" / CHILD
        root.parent.mkdir(parents=True)
        if failure == "symlink":
            root.symlink_to(tmp_path, target_is_directory=True)
        else:
            p = root / "research/module.py"
            p.parent.mkdir(parents=True)
            p.write_text("old evidence")
    with pytest.raises((ValueError, PermissionError)):
        m.dispatch(g, request)
    assert m.load()["assignments"]["prep"] == original
    assert all(method == "GET" for method, _ in g.calls)


def test_worker_cannot_collect(tmp_path, monkeypatch):
    m, g, request, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "is_main", lambda req: False)
    with pytest.raises(PermissionError):
        m.dispatch(g, request)
    assert not g.calls


def test_overlapping_batches_remain_usable_after_archive(tmp_path, monkeypatch):
    m, g, request, _ = fixture(tmp_path, monkeypatch)
    first = m.dispatch(g, {**request, "artifacts": request["artifacts"][:1]})
    m.dispatch(g, request)
    target = next(iter(first["evidence_paths"].values()))
    expected = request["artifacts"][0]["sha256"]
    assert m.collected_bytes(m.load(), target, expected)[1] == CHILD
    item = m.load()["assignments"]["prep"]
    raw = json.dumps(item).encode()
    p = tmp_path / f"orchestration/archive/prep-{m.digest(raw)}.json"
    p.parent.mkdir()
    p.write_bytes(raw)
    assert m.collected_bytes({"assignments": {}}, target, expected)[1] == CHILD


@pytest.mark.parametrize("failure", ["file", "ledger"])
def test_partial_write_failure_has_no_receipt_and_retries_exact_bytes(
    tmp_path, monkeypatch, failure
):
    m, g, request, original = fixture(tmp_path, monkeypatch)
    atomic = m.context_manager.atomic
    writes = []

    def failing_atomic(path, content):
        writes.append(str(path))
        if (failure == "file" and len(writes) == 2) or (
            failure == "ledger" and str(path) == str(m.STATE)
        ):
            raise OSError("injected storage failure")
        return atomic(path, content)

    with monkeypatch.context() as scoped:
        scoped.setattr(m.context_manager, "atomic", failing_atomic)
        with pytest.raises(OSError):
            m.dispatch(g, request)
    assert m.load()["assignments"]["prep"] == original
    receipt = m.dispatch(g, request)
    assert len(m.load()["assignments"]["prep"]["artifact_collections"]) == 1
    for source, target in receipt["evidence_paths"].items():
        assert (tmp_path / target).read_bytes() == g.data[source]


def test_total_limit_validates_before_any_write(tmp_path, monkeypatch):
    m, g, request, original = fixture(tmp_path, monkeypatch)
    g.data = {f"research/part-{i}.txt": b"x" * (1024 * 1024) for i in range(9)}
    request["artifacts"] = [{"path": k, "sha256": m.digest(v)} for k, v in g.data.items()]
    with pytest.raises(ValueError, match="total size"):
        m.dispatch(g, request)
    assert not (tmp_path / "orchestration/results").exists()
    assert m.load()["assignments"]["prep"] == original


def test_ancestor_chain_and_partial_batch_receipt_limit(tmp_path, monkeypatch):
    m, g, request, _ = fixture(tmp_path, monkeypatch)
    middle = "candidate-" + "e" * 32
    g.sessions[1]["parent"] = middle
    g.sessions.append({"session_id": middle, "parent": PRODUCER})
    receipt = m.dispatch(g, request)
    assert receipt["ancestry"] == [CHILD, middle, PRODUCER]
    state = m.load()
    state["assignments"]["prep"]["artifact_collections"] *= 16
    m.save(state)
    # Existing exact operation remains idempotent at the cap; different batch fails.
    m.dispatch(g, request)
    with pytest.raises(ValueError, match="receipt limit"):
        m.dispatch(g, {**request, "artifacts": request["artifacts"][:1]})


def test_reads_use_bounded_pages(tmp_path, monkeypatch):
    m, g, request, _ = fixture(tmp_path, monkeypatch)
    m.dispatch(g, request)
    artifact_calls = [p for method, p in g.calls if "/artifacts?" in p]
    assert artifact_calls and all(
        parse_qs(urlsplit(p).query)["limit"] == ["1048577"] for p in artifact_calls
    )
