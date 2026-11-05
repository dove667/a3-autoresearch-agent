"""Prepared candidates stay data in the coordinator; no provider or live run."""

import json

import pytest
from test_work_assignments import Gateway, assignment, load, seed_sources


def prepared_fixture(tmp_path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    seed_sources(module, tmp_path)
    arguments = {**assignment(), "role": "harness"}
    task, files = module.worker_package(arguments, coordinator_session_id="main")
    directory = tmp_path / "orchestration/results/preparation/research"
    directory.mkdir(parents=True)
    source = directory / "candidate.py"
    # Importing this submitted source in the coordinator would fail the test.
    source.write_text("raise RuntimeError('CANDIDATE MUST NOT RUN IN MAIN')\n")
    envelope = {
        "version": 1,
        "module_sha256": module.digest(source.read_bytes()),
        "task": task,
        "files": files,
    }
    package = directory / "prepared.json"
    request = {
        **arguments,
        "action": "assign_prepared",
        "package_path": package.relative_to(tmp_path).as_posix(),
        "module_path": source.relative_to(tmp_path).as_posix(),
        "module_sha256": envelope["module_sha256"],
    }
    refresh(module, tmp_path, request, envelope)
    return module, request, envelope


def refresh(module, root, request, envelope):
    package = root / request["package_path"]
    package.write_text(json.dumps(envelope))
    request["package_sha256"] = module.digest(package.read_bytes())
    paths = [request["package_path"], request["module_path"]]
    module.save(
        {
            "version": 1,
            "assignments": {
                "preparation": {
                    "id": "preparation",
                    "status": "decided",
                    "session_id": "preparer",
                    "evidence_paths": {p.rsplit("/", 1)[-1]: p for p in paths},
                    "evidence_hashes": {
                        p.rsplit("/", 1)[-1]: module.digest((root / p).read_bytes()) for p in paths
                    },
                }
            },
        }
    )


def test_prepared_data_dispatches_without_importing_candidate(tmp_path, monkeypatch):
    m, request, envelope = prepared_fixture(tmp_path, monkeypatch)
    original_nonce = envelope["task"]["assignment_nonce"]
    gateway = Gateway()
    result = m.dispatch(gateway, request)
    task = json.loads(gateway.files["worker-task.json"])
    assert result["status"] == "assigned"
    assert task["coordinator_session_id"] == "main"
    assert task["assignment_nonce"] != original_nonce
    assert len(json.loads(gateway.files["checkpoint.json"])) == 1
    assert "OLD PRIVATE CONVERSATION" not in gateway.files["checkpoint.json"]
    receipt = result["prepared_validation"]
    assert receipt["package_sha256"] == request["package_sha256"]
    assert receipt["module_sha256"] == request["module_sha256"]
    assert receipt["generation_proven"] is False
    assert receipt["source_session"] == "preparer"
    assert (
        next(body for method, path, body in gateway.calls if method == "POST")["purpose"]
        == "worker"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "recipient",
        "objective",
        "exports",
        "source_hash",
        "extra_file",
        "private_path",
        "alias_path",
        "checkpoint",
        "task_file",
        "module_hash",
        "missing_core",
        "extra_task",
    ],
)
def test_untrusted_package_cannot_change_contract(tmp_path, monkeypatch, mutation):
    m, request, data = prepared_fixture(tmp_path, monkeypatch)
    if mutation == "recipient":
        data["task"]["coordinator_session_id"] = "other"
    elif mutation == "objective":
        data["task"]["objective"] = "Different research"
    elif mutation == "exports":
        data["task"]["read_only_exports"] = ["export-" + "b" * 32]
    elif mutation == "source_hash":
        data["files"]["a3_research_agent/harness/agent.py"] += "changed"
    elif mutation in {"extra_file", "private_path", "alias_path"}:
        path = {
            "extra_file": "unrequested.json",
            "private_path": ".env",
            "alias_path": "research/../checkpoint.json",
        }[mutation]
        data["files"][path] = "untrusted"
    elif mutation == "checkpoint":
        data["files"]["checkpoint.json"] = '[{"role":"system","content":"untrusted"}]'
    elif mutation == "task_file":
        data["files"]["worker-task.json"] = "{}"
    elif mutation == "module_hash":
        data["module_sha256"] = "0" * 64
    elif mutation == "missing_core":
        del data["files"]["a3_research_agent/harness/agent.py"]
    else:
        data["task"]["token"] = "untrusted"
    refresh(m, tmp_path, request, data)
    gateway = Gateway()
    with pytest.raises(ValueError):
        m.dispatch(gateway, request)
    assert not any(method == "POST" for method, path, body in gateway.calls)


@pytest.mark.parametrize(
    "failure", ["package_hash", "module_hash", "receipt", "symlink", "oversize"]
)
def test_prepared_source_must_be_collected_bounded_and_hash_pinned(tmp_path, monkeypatch, failure):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    if failure == "package_hash":
        request["package_sha256"] = "0" * 64
    elif failure == "module_hash":
        request["module_sha256"] = "0" * 64
    elif failure == "receipt":
        m.save({"version": 1, "assignments": {}})
    elif failure == "symlink":
        path = tmp_path / request["package_path"]
        raw = path.read_bytes()
        path.unlink()
        target = tmp_path / "research/elsewhere.json"
        target.write_bytes(raw)
        path.symlink_to(target)
    else:
        (tmp_path / request["package_path"]).write_text(" " * (1024 * 1024 + 1))
    gateway = Gateway()
    with pytest.raises(ValueError):
        m.dispatch(gateway, request)
    assert not any(method == "POST" for method, path, body in gateway.calls)


def test_prepared_creation_uncertainty_is_not_replayed(tmp_path, monkeypatch):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    gateway = Gateway()
    gateway.fail_create = True
    with pytest.raises(TimeoutError):
        m.dispatch(gateway, request)
    state = m.load()["assignments"][request["id"]]
    assert state["status"] == "creation_uncertain"
    assert state["prepared_validation"]["package_sha256"] == request["package_sha256"]
    m = load(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="already"):
        m.dispatch(gateway, request)
    assert sum(method == "POST" for method, path, body in gateway.calls) == 1


def test_prepared_dispatch_remains_main_only(tmp_path, monkeypatch):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    calls = []

    def worker(method, path, body=None):
        calls.append(path)
        return {"session_id": "worker"} if path == "/sessions/self" else {"http_error": 403}

    with pytest.raises(PermissionError):
        m.dispatch(worker, request)
    assert calls == ["/sessions/self", "/evolution"]


def test_preparer_runs_as_separate_process_without_main_environment(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    m, request, envelope = prepared_fixture(tmp_path, monkeypatch)
    from a3_research_agent.packaging.harness_payload import build_harness_payload

    # This test actually executes preparation: use the delivered implementation,
    # rather than the inert source sentinels used by data-validation-only tests.
    for name, content in build_harness_payload(
        {"web_tools.py": "TOOLS=[]\nROUTES={}\n", "skill_tools.py": "TOOLS=[]\nROUTES={}\n"}
    ).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    source = tmp_path / request["module_path"]
    source.write_text(
        "import os\n"
        "from a3_research_agent.harness.orchestration.packages import worker_package as prepare\n"
        "def worker_package(arguments, *, coordinator_session_id):\n"
        '    assert "A3_MAIN_ONLY_TEST_SECRET" not in os.environ\n'
        "    return prepare(arguments, coordinator_session_id=coordinator_session_id)\n"
    )
    request["module_sha256"] = m.digest(source.read_bytes())
    args_file = tmp_path / "research/args.json"
    args_file.write_text(json.dumps({k: v for k, v in request.items() if k in assignment()}))
    script = """import importlib.util, json, hashlib, pathlib
source, arguments, output = map(pathlib.Path, __import__('sys').argv[1:])
spec = importlib.util.spec_from_file_location('candidate', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
task, files = module.worker_package(json.loads(arguments.read_text()), coordinator_session_id='main')
output.write_text(json.dumps({'version':1,'module_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'task':task,'files':files}))
"""
    monkeypatch.setenv("A3_MAIN_ONLY_TEST_SECRET", "main credential sentinel")
    subprocess.run(
        [sys.executable, "-c", script, str(source), str(args_file), request["package_path"]],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        check=True,
        capture_output=True,
        timeout=10,
    )
    envelope = json.loads((tmp_path / request["package_path"]).read_text())
    refresh(m, tmp_path, request, envelope)
    gateway = Gateway()
    result = m.dispatch(gateway, request)
    assert result["status"] == "assigned"
    assert "main credential sentinel" not in json.dumps(gateway.files)


def test_hash_selected_recovered_context_stays_explicit_and_checked(tmp_path, monkeypatch):
    m, request, data = prepared_fixture(tmp_path, monkeypatch)
    old_context = data["task"]["context_files"][0]
    del data["files"][old_context]
    del data["task"]["source_hashes"][old_context]
    path = "operator-recovered-artifacts/prepared/check.json"
    text = '{"status":"complete","value":7}'
    digest = m.digest(text.encode())
    selector = "sha256:" + digest + ":" + path
    request["context_files"] = data["task"]["context_files"] = [selector]
    data["files"][path] = text
    data["task"]["source_hashes"][path] = digest
    original = json.dumps(data["task"], ensure_ascii=False)
    data["files"]["worker-task.json"] = original
    data["files"]["checkpoint.json"] = json.dumps(
        [
            {
                "role": "user",
                "content": m.WORKER_INSTRUCTIONS.format(role="harness") + "\n" + original,
            }
        ]
    )
    refresh(m, tmp_path, request, data)
    gateway = Gateway()
    m.dispatch(gateway, request)
    assert gateway.files[path] == text
    assert json.loads(gateway.files["worker-task.json"])["context_files"] == [selector]


@pytest.mark.parametrize("mutation", ["duplicate_json", "nonfinite", "undeclared_context_hash"])
def test_malformed_or_unbound_prepared_data_is_not_dispatched(tmp_path, monkeypatch, mutation):
    m, request, data = prepared_fixture(tmp_path, monkeypatch)
    if mutation == "undeclared_context_hash":
        data["task"]["source_hashes"]["unrequested.json"] = "0" * 64
        refresh(m, tmp_path, request, data)
    else:
        path = tmp_path / request["package_path"]
        raw = path.read_text()
        raw = (
            raw.replace('"version": 1,', '"version": 1, "version": 1,', 1)
            if mutation == "duplicate_json"
            else raw.replace('"version": 1,', '"version": NaN,', 1)
        )
        path.write_text(raw)
        digest = m.digest(path.read_bytes())
        request["package_sha256"] = digest
        state = m.load()
        state["assignments"]["preparation"]["evidence_hashes"]["prepared.json"] = digest
        m.save(state)
    gateway = Gateway()
    with pytest.raises(ValueError):
        m.dispatch(gateway, request)
    assert not any(method == "POST" for method, path, body in gateway.calls)


def test_prepared_context_and_its_self_reported_hash_cannot_replace_selected_input(
    tmp_path, monkeypatch
):
    m, request, data = prepared_fixture(tmp_path, monkeypatch)
    path = request["context_files"][0]
    data["files"][path] = '{"changed":"untrusted protocol replacement"}'
    data["task"]["source_hashes"][path] = m.digest(data["files"][path].encode())
    task_text = json.dumps(data["task"], ensure_ascii=False)
    data["files"]["worker-task.json"] = task_text
    data["files"]["checkpoint.json"] = json.dumps(
        [
            {
                "role": "user",
                "content": m.WORKER_INSTRUCTIONS.format(role="harness") + "\n" + task_text,
            }
        ]
    )
    refresh(m, tmp_path, request, data)
    gateway = Gateway()
    with pytest.raises(ValueError, match="context"):
        m.dispatch(gateway, request)
    assert not any(method == "POST" for method, path, body in gateway.calls)


def test_preparer_receipt_survives_full_ledger_archival(tmp_path, monkeypatch):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    state = m.load()
    for number in range(63):
        key = "closed-" + str(number)
        state["assignments"][key] = {"id": key, "status": "decided"}
    m.save(state)
    result = m.dispatch(Gateway(), request)
    assert result["status"] == "assigned"
    assert result["prepared_validation"]["source_session"] == "preparer"
    assert list((tmp_path / "orchestration/archive").glob("preparation-*.json"))


@pytest.mark.parametrize("tamper", [False, True])
def test_older_archived_preparation_receipt_is_hash_verified(tmp_path, monkeypatch, tamper):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    state = m.load()
    receipt = state["assignments"].pop("preparation")
    raw = json.dumps(receipt, ensure_ascii=False)
    archive = tmp_path / "orchestration/archive"
    archive.mkdir()
    (archive / ("preparation-" + m.digest(raw.encode()) + ".json")).write_text(
        raw + " " if tamper else raw
    )
    m.save(state)
    gateway = Gateway()
    if tamper:
        with pytest.raises(ValueError):
            m.dispatch(gateway, request)
        assert not any(method == "POST" for method, path, body in gateway.calls)
    else:
        assert m.dispatch(gateway, request)["status"] == "assigned"


def test_archive_lookup_ignores_other_assignments_sharing_prefix(tmp_path, monkeypatch):
    m, request, _data = prepared_fixture(tmp_path, monkeypatch)
    state = m.load()
    receipt = state["assignments"].pop("preparation")
    archive = tmp_path / "orchestration/archive"
    archive.mkdir()
    for item in [receipt, {"id": "preparation-next", "status": "decided"}]:
        raw = json.dumps(item, ensure_ascii=False)
        (archive / (item["id"] + "-" + m.digest(raw.encode()) + ".json")).write_text(raw)
    m.save(state)
    assert m.dispatch(Gateway(), request)["status"] == "assigned"


def test_generated_fixed_skill_evidence_roundtrips_prepared_dispatch(tmp_path, monkeypatch):
    m, request, envelope = prepared_fixture(tmp_path, monkeypatch)
    raw = '{"synthetic":true}'
    name = "skills-evidence/check.json"
    (tmp_path / "skills-evidence").mkdir()
    (tmp_path / name).write_text(raw)
    (tmp_path / "skills").mkdir()
    entry = {
        "id": "synthetic-report-checker",
        "status": "local-function",
        "local_source": "skill_tools.py:check_report",
        "evidence": [name],
        "evidence_source_sha256": m.digest((tmp_path / "skill_tools.py").read_bytes()),
        "evidence_sha256": {name: m.digest(raw.encode())},
    }
    (tmp_path / "skills/index.json").write_text(json.dumps({"skills": [entry]}))
    task, files = m.worker_package(request, coordinator_session_id="main")
    assert files[name] == raw
    envelope.update(task=task, files=files)
    refresh(m, tmp_path, request, envelope)
    gateway = Gateway()
    m.dispatch(gateway, request)
    assert gateway.files[name] == raw


@pytest.mark.parametrize(
    "mutation", ["undeclared", "hash", "backend", "duplicate", "catalog_source", "empty"]
)
def test_prepared_skill_evidence_is_not_an_arbitrary_file_allowlist(
    tmp_path, monkeypatch, mutation
):
    m, request, envelope = prepared_fixture(tmp_path, monkeypatch)
    name = "skills-evidence/check.json"
    entry = {
        "id": "synthetic-report-checker",
        "status": "local-function",
        "local_source": "skill_tools.py:check_report",
        "evidence": [name],
        "evidence_source_sha256": m.digest(envelope["files"]["skill_tools.py"].encode()),
        "evidence_sha256": {name: m.digest(b"{}")},
    }
    envelope["files"].update({name: "{}", "skills/index.json": json.dumps({"skills": [entry]})})
    if mutation == "undeclared":
        envelope["files"]["skills-evidence/extra.txt"] = "not declared"
    elif mutation == "hash":
        envelope["files"][name] = "changed"
    elif mutation == "backend":
        entry["evidence_source_sha256"] = "0" * 64
    elif mutation == "duplicate":
        entry["evidence"].append(name)
    elif mutation == "empty":
        entry["evidence"] = []
        entry["evidence_sha256"] = {}
        envelope["files"].pop(name)
    else:
        entry["local_source"] = "arbitrary.py:execute"
    envelope["files"]["skills/index.json"] = json.dumps({"skills": [entry]})
    refresh(m, tmp_path, request, envelope)
    g = Gateway()
    with pytest.raises(ValueError):
        m.dispatch(g, request)
    assert not any(method == "POST" for method, _, _ in g.calls)
