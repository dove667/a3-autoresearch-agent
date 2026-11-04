"""Isolated worker contracts; no provider, training, GPU or live run."""

import base64
import importlib.util
import json
import pathlib

import pytest
from harness_sources import HarnessSources

ROOT = HarnessSources(pathlib.Path(__file__).resolve().parents[2] / "runtime/harness_recovery")


def load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    path = ROOT / "work_assignments.py"
    assert path.exists(), "Explicit coordinator/worker contract is not implemented"
    spec = importlib.util.spec_from_file_location("assignments", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    def __init__(self):
        self.calls = []
        self.files = {}
        self.active = False
        self.fail_create = False

    def __call__(self, method, path, data=None, **options):
        self.calls.append((method, path, data))
        if path == "/sessions/self":
            return {"session_id": "main"}
        if path == "/evolution":
            return {"main_owner": "main"}
        if path == "/sessions?include_status=true":
            return {
                "sessions": [
                    {
                        "session_id": "worker-1",
                        "status": {"status": "running" if self.active else "exited"},
                    }
                ]
            }
        if method == "POST" and path == "/sessions":
            self.files = data["files"]
            if self.fail_create:
                raise TimeoutError("uncertain response")
            return {"session_id": "worker-1"}
        if "/artifacts?" in path:
            from urllib.parse import parse_qs, urlparse

            name = parse_qs(urlparse(path).query)["path"][0]
            if name not in self.files:
                return {"http_error": 404}
            return {"data": base64.b64encode(self.files[name].encode()).decode()}
        if path == "/sessions/worker-1":
            return {"status": "exited", "exit_code": 0}
        raise AssertionError((method, path))


def seed_sources(module, root):
    for name in module.CORE_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("# source " + name)
    (root / "checkpoint.json").write_text('[{"role":"user","content":"OLD PRIVATE CONVERSATION"}]')
    (root / "research").mkdir()
    (root / "research/plan.json").write_text('{"hypothesis":"bounded fixture"}')


def assignment():
    return {
        "action": "assign",
        "id": "policy-check",
        "role": "policy",
        "objective": "Evaluate existing fixed fixture",
        "deliverables": ["result.json"],
        "stop_condition": "Return exact fixture result then stop",
        "context_files": ["research/plan.json"],
        "read_only_exports": ["export-" + "a" * 32],
    }


def test_worker_snapshot_has_fresh_context_and_explicit_source_hashes(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    request = {**assignment(), "coordinator_session_id": "untrusted-model-recipient"}
    result = m.dispatch(gateway, request)
    assert result["session_id"] == "worker-1"
    submitted = next(body for method, path, body in gateway.calls if method == "POST")
    assert submitted["purpose"] == "worker" and submitted["read_only_exports"] == [
        "export-" + "a" * 32
    ]
    assert "--worker-task" in submitted["command"]
    assert "OLD PRIVATE CONVERSATION" not in submitted["files"]["checkpoint.json"]
    task = json.loads(submitted["files"]["worker-task.json"])
    assert task["role"] == "policy" and task["id"] == "policy-check"
    assert task["coordinator_session_id"] == "main"
    assert task["source_hashes"]["agent.py"]
    assert submitted["files"]["research/plan.json"] == (tmp_path / "research/plan.json").read_text()
    with pytest.raises(ValueError, match="already"):
        m.dispatch(gateway, assignment())
    assert sum(path == "/sessions" for _, path, _ in gateway.calls) == 1


def test_uncertain_creation_is_preserved_not_replayed(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    gateway.fail_create = True
    with pytest.raises(TimeoutError):
        m.dispatch(gateway, assignment())
    m = load(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="already"):
        m.dispatch(gateway, assignment())
    assert sum(path == "/sessions" for _, path, _ in gateway.calls) == 1


def test_result_is_verified_and_decision_does_not_forge_acceptance(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    import hashlib

    gateway.files["result.json"] = '{"fixture":true}'
    gateway.files["worker-result.json"] = json.dumps(
        {
            "id": "policy-check",
            "role": "policy",
            "status": "complete",
            "evidence": [
                {
                    "path": "result.json",
                    "sha256": hashlib.sha256(gateway.files["result.json"].encode()).hexdigest(),
                }
            ],
            "summary": "Fixture only; no policy gain claimed",
            "exports": [],
        }
    )
    result = m.dispatch(gateway, {"action": "collect", "id": "policy-check"})
    assert result["status"] == "awaiting_decision"
    assert result["evidence_paths"] == {
        "result.json": "orchestration/results/policy-check/result.json"
    }
    assert result["evidence_source_session"] == "worker-1"
    assert gateway.files["worker-result.json"] == json.dumps(result["result"])
    decided = m.dispatch(
        gateway,
        {
            "action": "decide",
            "id": "policy-check",
            "decision": "reject",
            "reason": "Fixture is not research evidence",
        },
    )
    assert decided["decision"] == "reject"
    assert not (tmp_path / "run-outcome.json").exists()
    assert "result.json" in decided["evidence_hashes"]


def test_main_job_execution_is_rejected_without_sending_job(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    gateway = Gateway()
    with pytest.raises(PermissionError, match="delegate"):
        m.guard_gateway(gateway, "POST", "/jobs", {"command": ["python", "train.py"], "gpu": True})
    assert not any(path == "/jobs" for _, path, _ in gateway.calls)
    m.guard_gateway(gateway, "GET", "/jobs/known", None)


def test_handoff_waits_for_old_workers_without_cancelling_them(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    gateway = Gateway()
    gateway.active = True
    with pytest.raises(ValueError, match="worker"):
        m.guard_gateway(gateway, "POST", "/evolution/trials/candidate/decision", {"retain": True})
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)
    gateway.active = False
    m.guard_gateway(gateway, "POST", "/evolution/trials/candidate/decision", {"retain": True})


def test_worker_can_submit_own_job_when_evolution_is_correctly_forbidden(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)

    def worker_request(method, path, data=None):
        return {"session_id": "worker"} if path == "/sessions/self" else {"http_error": 403}

    m.guard_gateway(
        worker_request, "POST", "/jobs", {"command": ["python", "fixture.py"], "gpu": False}
    )


def test_input_reference_is_discoverable_and_worker_copy_matches_source(tmp_path, monkeypatch):
    import hashlib

    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    inputs = tmp_path / "inputs"
    source = inputs / "skills/research-orchestrator/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: research-orchestrator\ndescription: Flexible coordination\n---\nMethods are optional."
    )
    entry = source.parent / "assets/catalog-entry.json"
    entry.parent.mkdir()
    entry.write_text(
        json.dumps(
            {
                "id": "research-orchestrator",
                "version": "0.1.1",
                "status": "reference",
                "local_source": "skills/research-orchestrator/SKILL.md",
                "evidence": [],
            }
        )
    )
    files = {
        str(p.relative_to(inputs)): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in [source, entry]
    }
    (inputs / "input-manifest.json").write_text(
        json.dumps({"status": "active", "revision": "r1", "files": files})
    )
    m.install_input_skills(inputs)
    first = (tmp_path / "skills/index.json").read_bytes()
    m.install_input_skills(inputs)
    assert (tmp_path / "skills/index.json").read_bytes() == first
    _, package = m.worker_package(assignment())
    catalog = json.loads(package["skills/index.json"])
    skill = next(e for e in catalog["skills"] if e["id"] == "research-orchestrator")
    assert skill["status"] == "reference"
    assert package[skill["local_source"]] == source.read_text()
    assert skill["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("path", ["/jobs?label=fixture", "/jobs/"])
def test_coordinator_job_guard_also_covers_equivalent_urls(tmp_path, monkeypatch, path):
    m = load(tmp_path, monkeypatch)
    with pytest.raises(PermissionError):
        m.guard_gateway(
            Gateway(), "POST", path, {"command": ["python", "fixture.py"], "gpu": False}
        )


def test_failed_worker_missing_report_can_be_rejected_without_fake_result(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    result = m.dispatch(gateway, {"action": "collect", "id": "policy-check"})
    assert result["status"] == "awaiting_decision"
    assert result["delivery_failed"] and result["scientific_success"] is None
    with pytest.raises(ValueError, match="adopt"):
        m.dispatch(
            gateway,
            {"action": "decide", "id": "policy-check", "decision": "adopt", "reason": "No report"},
        )
    result = m.dispatch(
        gateway,
        {
            "action": "decide",
            "id": "policy-check",
            "decision": "reject",
            "reason": "Worker exited without required result; assets remain unknown",
        },
    )
    assert result["decision"] == "reject"
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


@pytest.mark.parametrize(
    "objective",
    [
        "Evaluate existing fixed fixture",
        "A different research question with the same implementation",
    ],
)
def test_reconcile_never_binds_archived_same_id_to_a_new_question(tmp_path, monkeypatch, objective):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    old_files = dict(gateway.files)
    ledger = m.load()
    ledger["assignments"].pop("policy-check")
    ledger["archived_count"] = 1
    m.save(ledger)
    changed = {**assignment(), "objective": objective}
    gateway.fail_create = True
    with pytest.raises(TimeoutError):
        m.dispatch(gateway, changed)
    gateway.files = old_files  # No new session was actually created; only the old worker is listed.
    result = m.dispatch(gateway, {"action": "reconcile", "id": "policy-check"})
    assert result["creation_confirmed"] is False
    assert not result.get("session_id")


def test_successful_worker_identity_survives_post_creation_ledger_failure(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    save = m.save
    count = [0]

    def fail_after_creation(state):
        count[0] += 1
        if count[0] == 2:
            raise OSError("controlled disk failure")
        save(state)

    from a3_research_agent.harness.orchestration import dispatch as dispatch_module

    monkeypatch.setattr(dispatch_module, "save", fail_after_creation)
    result = m.dispatch(gateway, assignment())
    assert result["session_id"] == "worker-1"
    assert result["ledger_persisted"] is False
    monkeypatch.setattr(dispatch_module, "save", save)
    recovered = m.dispatch(gateway, {"action": "reconcile", "id": "policy-check"})
    assert recovered["session_id"] == "worker-1"
    assert sum(path == "/sessions" for _, path, _ in gateway.calls) == 1


def test_existing_allowlisted_local_skill_keeps_its_verified_source_and_evidence(
    tmp_path, monkeypatch
):
    import hashlib

    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    evidence = tmp_path / "skills-evidence/checker.json"
    evidence.parent.mkdir()
    evidence.write_text('{"synthetic":true}')
    entry = {
        "id": "synthetic-report-checker",
        "version": "1.2.0",
        "status": "local-function",
        "local_source": "skill_tools.py:check_report",
        "evidence": ["skills-evidence/checker.json"],
        "evidence_source_sha256": hashlib.sha256(
            (tmp_path / "skill_tools.py").read_bytes()
        ).hexdigest(),
        "evidence_sha256": {
            "skills-evidence/checker.json": hashlib.sha256(evidence.read_bytes()).hexdigest()
        },
    }
    catalog = tmp_path / "skills/index.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({"skills": [entry]}))
    _, package = m.worker_package(assignment())
    assert json.loads(package["skills/index.json"])["skills"] == [entry]
    assert package["skills-evidence/checker.json"] == evidence.read_text()


@pytest.mark.parametrize("bad_delivery", ["invalid_json", "bad_hash"])
def test_stopped_invalid_delivery_can_be_explicitly_rejected_without_adoption(
    tmp_path, monkeypatch, bad_delivery
):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    gateway.files["worker-result.json"] = (
        "not-json"
        if bad_delivery == "invalid_json"
        else json.dumps(
            {
                "id": "policy-check",
                "role": "policy",
                "status": "complete",
                "evidence": [{"path": "result.json", "sha256": "f" * 64}],
                "exports": [],
            }
        )
    )
    gateway.files["result.json"] = "unchanged raw evidence"
    with pytest.raises(ValueError):
        m.dispatch(gateway, {"action": "collect", "id": "policy-check"})
    result = m.dispatch(
        gateway,
        {
            "action": "decide",
            "id": "policy-check",
            "decision": "reject",
            "reason": "Required delivery is invalid; underlying experiment outcome remains unknown",
        },
    )
    assert result["decision"] == "reject" and result["scientific_success"] is None
    assert not any(method == "DELETE" for method, _, _ in gateway.calls)


def test_collecting_closed_assignment_does_not_reopen_its_decision(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    m.dispatch(
        gateway,
        {
            "action": "decide",
            "id": "policy-check",
            "decision": "reject",
            "reason": "No delivered evidence",
        },
    )
    before = len(gateway.calls)
    result = m.dispatch(gateway, {"action": "collect", "id": "policy-check"})
    assert result["status"] == "decided" and result["decision"] == "reject"
    assert not any("/artifacts" in path for _, path, _ in gateway.calls[before:])


def test_former_coordinator_retires_but_active_coordinator_and_workers_do_not(
    tmp_path, monkeypatch
):
    m = load(tmp_path, monkeypatch)

    def former(method, path, data=None):
        return {"is_main": False, "has_been_main": True, "session_id": "old"}

    assert m.should_retire(former, None)
    assert not m.should_retire(former, {"role": "policy"})
    with pytest.raises(PermissionError):
        m.guard_gateway(former, "POST", "/jobs", {})
    assert not m.should_retire(lambda *args: {"is_main": True, "has_been_main": True}, None)


def test_worker_report_cannot_expand_model_context_with_arbitrary_fields(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    gateway.files["worker-result.json"] = json.dumps(
        {
            "id": "policy-check",
            "role": "policy",
            "status": "blocked",
            "summary": "Original details are in evidence files",
            "evidence": [],
            "raw_log": "x" * 100000,
        }
    )
    with pytest.raises(ValueError, match="bounded report"):
        m.dispatch(gateway, {"action": "collect", "id": "policy-check"})
    assert m.load()["assignments"]["policy-check"]["status"] != "awaiting_decision"


@pytest.mark.parametrize("summary", ["x" * 4001, {"nested": "not text"}])
def test_worker_report_requires_bounded_text_summary(tmp_path, monkeypatch, summary):
    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    gateway = Gateway()
    m.dispatch(gateway, assignment())
    gateway.files["worker-result.json"] = json.dumps(
        {
            "id": "policy-check",
            "role": "policy",
            "status": "blocked",
            "summary": summary,
            "evidence": [],
        }
    )
    with pytest.raises(ValueError, match="bounded report"):
        m.dispatch(gateway, {"action": "collect", "id": "policy-check"})


@pytest.mark.parametrize("remote_path", ["/outputs/audit/raw.json", "../other/raw.json"])
def test_invalid_worker_delivery_explains_local_evidence_boundary(
    tmp_path, monkeypatch, remote_path
):
    m = load(tmp_path, monkeypatch)
    task = {"id": "audit", "role": "harness", "deliverables": ["research/audit.json"]}
    (tmp_path / "worker-result.json").write_text(
        json.dumps(
            {
                "id": "audit",
                "role": "harness",
                "status": "blocked",
                "evidence": [{"path": remote_path, "sha256": "a" * 64}],
            }
        )
    )
    result = m.worker_delivery_status(task)
    assert not result["ready"] and result["status"] == "invalid"
    assert "workspace-relative" in result["reason"]
    assert not m.worker_result_ready(task)


def test_worker_delivery_reports_hash_mismatch_without_altering_evidence(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    task = {"id": "audit", "role": "harness", "deliverables": []}
    (tmp_path / "evidence.json").write_text("original")
    report = {
        "id": "audit",
        "role": "harness",
        "status": "blocked",
        "evidence": [{"path": "evidence.json", "sha256": "a" * 64}],
    }
    (tmp_path / "worker-result.json").write_text(json.dumps(report))
    result = m.worker_delivery_status(task)
    assert result["status"] == "invalid" and "hash" in result["reason"]
    assert (tmp_path / "evidence.json").read_text() == "original"
    assert json.loads((tmp_path / "worker-result.json").read_text()) == report


@pytest.mark.parametrize("availability", ["valid", "missing", "corrupt"])
def test_collect_reports_cited_archive_delivery_without_rewriting_report(
    tmp_path, monkeypatch, availability
):
    import hashlib

    m = load(tmp_path, monkeypatch)
    seed_sources(m, tmp_path)
    g = Gateway()
    m.dispatch(g, assignment())
    raw = json.dumps(
        {"type": "function_call_output", "call_id": "worker-call", "output": "raw log"}
    )
    sha = hashlib.sha256(raw.encode()).hexdigest()
    name = f"context-history/tool-output-{sha}.json"
    report = "Observed data; original console " + name
    g.files["report.md"] = report
    if availability != "missing":
        g.files[name] = raw if availability == "valid" else "CORRUPT"
    g.files["worker-result.json"] = json.dumps(
        {
            "id": "policy-check",
            "role": "policy",
            "status": "blocked",
            "summary": "partial",
            "evidence": [
                {"path": "report.md", "sha256": hashlib.sha256(report.encode()).hexdigest()}
            ],
            "exports": [],
        }
    )
    result = m.dispatch(g, {"action": "collect", "id": "policy-check"})
    assert (tmp_path / "orchestration/results/policy-check/report.md").read_text() == report
    if availability == "valid":
        assert result["reference_hashes"][name] == sha
        assert (tmp_path / result["evidence_paths"][name]).read_text() == raw
    else:
        assert name not in result["reference_hashes"]
        assert result["reference_delivery"][name]["status"] == "unavailable"
    assert not (tmp_path / "run-outcome.json").exists()


def test_optional_reference_overflow_keeps_one_aggregate_record(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    calls = []

    def request(method, path, data=None):
        calls.append(path)
        return {"http_error": 404}

    text = " ".join("context-history/tool-output-" + format(i, "064x") + ".json" for i in range(12))
    hashes, delivery = m.collect_report_references(request, "worker", "assignment", [text], {})
    assert not hashes and len(calls) == 8 and len(delivery) == 9
    assert delivery["__omitted__"]["count"] == 4
