"""Real worker loops and Host routes with a scripted provider and local processes.

The process sandbox maps the container's /workspace root to a private temporary
directory using test-only sitecustomize. This exercises the real tool guard but
does not prove Docker isolation. Web/skill stubs provide no E/F capability proof.
"""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runtime.local.candidate_runtime import CandidateRuntime
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "runtime/harness_recovery"
OLD_MAIN = "OLD-MAIN-CONVERSATION-MUST-NOT-ENTER-WORKER"
WORKER_MODEL = "chatgpt/gpt-6.1-sol"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ProcessSandbox:
    def __init__(self, root, versions):
        self.root, self.versions = root, versions
        self.lock = threading.RLock()
        self.records, self.tokens, self.launches = {}, {}, {}
        self.endpoint = None
        self.support = root / "process-support"
        self.support.mkdir(parents=True)
        (self.support / "sitecustomize.py").write_text(
            "import os, pathlib\n"
            "OriginalPath = pathlib.Path\n"
            "class WorkspacePath(type(OriginalPath())):\n"
            "    def __new__(cls, *parts, **kwargs):\n"
            "        if parts and str(parts[0]) == '/workspace':\n"
            "            parts = (os.getcwd(), *parts[1:])\n"
            "        return super().__new__(cls, *parts, **kwargs)\n"
            "pathlib.Path = WorkspacePath\n"
        )

    def create_version(self, owner, command, token, version_id):
        files = self.versions.read_snapshot(self.versions.root, version_id)
        directory = self.root / owner / "workspace"
        directory.mkdir(parents=True)
        for name, text in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.tokens[owner] = token
        self.launches[owner] = files
        output = (directory / "process-output.log").open("wb")
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(self.support),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "A3_GATEWAY_URL": self.endpoint,
            "A3_GATEWAY_TOKEN": token,
            "CONTEXT_WINDOW_ESTIMATE": "1000000",
            "CONTEXT_RESERVE_ESTIMATE": "10000",
        }
        try:
            process = subprocess.Popen(
                [sys.executable, *command[1:]],
                cwd=directory,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        finally:
            output.close()
        self.records[owner] = {"owner": owner, "directory": directory, "process": process}
        return {"session_id": owner, "version_id": version_id, "status": "created"}

    def owned(self, owner, session):
        if owner != session or session not in self.records:
            raise PermissionError("Test process is not caller-owned")
        return self.records[session]

    read_workspace_files = CandidateRuntime.read_workspace_files

    def status(self, owner, session):
        assert owner == session
        if session == "main":
            return {"session_id": session, "status": "running"}
        record = self.records[session]
        code = record["process"].poll()
        return {
            "session_id": session,
            "status": "running" if code is None else "exited",
            "exit_code": code,
        }

    def artifact(self, owner, session, name, offset=0, limit=None):
        assert owner == session
        root = self.records[session]["directory"]
        target = (root / name).resolve()
        if root not in target.parents:
            raise PermissionError("Outside test session")
        raw = target.read_bytes()
        selected = raw[offset:] if limit is None else raw[offset : offset + limit]
        return {"path": name, "encoding": "base64", "data": base64.b64encode(selected).decode()}

    def close(self):
        for record in self.records.values():
            process = record["process"]
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@pytest.fixture
def pipeline(tmp_path, monkeypatch, request):
    invalid_delivery = getattr(request, "param", False) is True
    main = tmp_path / "main"
    main.mkdir()
    monkeypatch.chdir(main)
    monkeypatch.syspath_prepend(str(CORE))
    spec = importlib.util.spec_from_file_location(
        "pipeline_assignments", CORE / "work_assignments.py"
    )
    assignments = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assignments)
    for name in assignments.CORE_FILES:
        target = main / name
        if name in {"web_tools.py", "skill_tools.py"}:
            target.write_text("TOOLS = []\nROUTES = {}\n")
        else:
            source = (
                ROOT / "runtime/local/token_count.py" if name == "token_count.py" else CORE / name
            )
            target.write_bytes(source.read_bytes())
    (main / "checkpoint.json").write_text(json.dumps([{"role": "user", "content": OLD_MAIN}]))
    (main / "research").mkdir()
    (main / "research/context.json").write_text('{"fixture":"synthetic evidence only"}')
    versions = CandidateVersions(tmp_path / "versions")
    sandbox = ProcessSandbox(tmp_path / "processes", versions)
    provider_calls, remote_calls, bridge_errors = [], [], []
    rendezvous = threading.Barrier(2)
    export_records = {}

    def provider(**request):
        assert request["model"] == WORKER_MODEL
        assert request["reasoning"] == {"effort": "medium"}
        assert OLD_MAIN not in json.dumps(request)
        task = json.loads(request["input"][0]["content"].split("\n", 1)[1])
        role = task["role"]
        assert "bounded " + role + " worker" in request["instructions"]
        assert "not the main coordinator" in request["instructions"]
        turn = 1 + sum(entry["role"] == role for entry in provider_calls)
        provider_calls.append({"role": role, "request": request, "repair": turn > 1})
        rendezvous.wait(timeout=10)
        evidence = json.dumps({"role": role, "fixture": "synthetic, no model/GPU experiment"})
        job_id = ("a" if role == "policy" else "b") * 32
        export_id = "export-" + job_id
        report = {
            "id": task["id"],
            "role": role,
            "status": "complete",
            "summary": "Synthetic worker-loop evidence; no scientific improvement claim",
            "evidence": [{"path": "research/result.json", "sha256": sha(evidence)}],
            "exports": [export_id],
        }
        if invalid_delivery and turn == 2:
            frames = [
                json.loads(entry["content"])
                for entry in request["input"]
                if isinstance(entry.get("content"), str) and entry["content"].startswith("{")
            ]
            delivery = frames[-1]["worker_delivery"]
            assert delivery["status"] == "invalid" and "workspace-relative" in delivery["reason"]
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": role + "repair",
                        "name": "write",
                        "arguments": json.dumps(
                            {"path": "worker-result.json", "content": json.dumps(report)}
                        ),
                    }
                ],
            }
        if invalid_delivery:
            report["evidence"][0]["path"] = "/outputs/fixture/result.json"
        operations = [
            ("gateway", {"method": "GET", "path": "/evolution"}),
            ("write", {"path": str(tmp_path / "outside-worker.txt"), "content": "must be denied"}),
            (
                "gateway",
                {
                    "method": "POST",
                    "path": "/jobs",
                    "body": {
                        "command": ["synthetic-fixture"],
                        "gpu": False,
                        "files": {"role.txt": role},
                    },
                },
            ),
            (
                "gateway",
                {
                    "method": "POST",
                    "path": "/jobs/" + job_id + "/exports",
                    "body": {"location": "outputs", "path": "fixture"},
                },
            ),
            (
                "gateway",
                {
                    "method": "POST",
                    "path": "/jobs/exports/" + export_id + "/grants",
                    "body": {"session_id": task["coordinator_session_id"]},
                },
            ),
            ("write", {"path": "research/result.json", "content": evidence}),
            ("write", {"path": "worker-result.json", "content": json.dumps(report)}),
        ]
        return {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": role + str(i),
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
                for i, (name, arguments) in enumerate(operations)
            ],
        }

    def remote(operation, request):
        # All remote work is a bounded fake; even a mistaken call cannot reach SSH.
        actor = request["experiment_id"]
        if operation == "submit":
            assert actor in sandbox.records
            assert request["command"] == ["synthetic-fixture"] and not request["gpu"]
            remote_calls.append(request)
            job_id = ("a" if request["files"]["role.txt"] == "policy" else "b") * 32
            return {"job_id": job_id, "status": "completed"}
        if operation == "export-create":
            identifier = "export-" + request["job_id"]
            export_records[identifier] = {"owner": actor, "readers": {actor}}
            return {"export_id": identifier, "status": "ready"}
        identifier = request["export_id"]
        record = export_records[identifier]
        assert actor in record["readers"], "Main cannot read an ungranted worker export"
        if operation == "export-grant":
            assert actor == record["owner"] and request["recipient"] == "main"
            record["readers"].add(request["recipient"])
            return {
                "export_id": identifier,
                "recipient": request["recipient"],
                "access": "read_only",
            }
        assert operation == "export-status", (operation, request)
        return {"export_id": identifier, "status": "ready"}

    restored = {
        "sessions": {"main-token": "main"},
        "root_owners": ["bootstrap"],
        "children": {
            "main": {"owner": "main", "parent": "bootstrap", "token": "main-token", "closed": False}
        },
        "scope_parents": {"main": "bootstrap"},
        "jobs": {},
        "evolution": {"main_owner": "main", "trials": {}, "transitions": []},
    }
    app = create_app(
        model_name="fixture/main",
        sessions={},
        main_owner="main",
        sandbox=sandbox,
        versions=versions,
        restored_state=restored,
        forward_model=provider,
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
        phase_instructions="MAIN-PHASE-NOT-AUTOMATIC-WORKER-CONTEXT",
        role_models={
            "worker": {
                "model_name": WORKER_MODEL,
                "request_options": {"reasoning": {"effort": "medium"}},
            }
        },
    )
    with TestClient(app) as client:

        class Bridge(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    response = client.request(
                        self.command,
                        self.path,
                        content=raw or None,
                        headers={
                            "Authorization": self.headers.get("Authorization", ""),
                            "Content-Type": "application/json",
                        },
                    )
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception as error:  # noqa: BLE001 - surface thread assertions to pytest
                    bridge_errors.append(error)
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"synthetic bridge failure"}')

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sandbox.endpoint = "http://127.0.0.1:" + str(server.server_port)

        def req(method, path, data=None, **kwargs):
            response = client.request(
                method, path, json=data, headers={"Authorization": "Bearer main-token"}
            )
            return (
                response.json()
                if response.is_success
                else {"http_error": response.status_code, "body": response.text}
            )

        try:
            yield assignments, req, sandbox, provider_calls, remote_calls, bridge_errors, client
        finally:
            rendezvous.abort()
            sandbox.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


@pytest.mark.parametrize("pipeline", [False, True, "prepared"], indirect=True)
def test_real_policy_and_harness_worker_loops_route_collect_and_decide(pipeline, tmp_path, request):
    assignments, req, sandbox, provider_calls, remote_calls, bridge_errors, client = pipeline
    assigned = {}
    for role in ("policy", "harness"):
        arguments = {
            "action": "assign",
            "id": role + "-fixture",
            "role": role,
            "objective": "Return synthetic evidence without real experiments",
            "stop_condition": "Write the fixture report and stop",
            "deliverables": ["research/result.json"],
            "context_files": ["research/context.json"],
        }
        if request.node.callspec.params["pipeline"] == "prepared" and role == "harness":
            task, files = assignments.worker_package(arguments, coordinator_session_id="main")
            directory = Path("orchestration/results/preparer/research")
            directory.mkdir(parents=True)
            source = directory / "candidate.py"
            source.write_text("raise RuntimeError('Do not import submitted code in main')")
            package = directory / "package.json"
            package.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "module_sha256": sha(source.read_text()),
                        "task": task,
                        "files": files,
                    }
                )
            )
            state = assignments.load()
            state["assignments"]["preparer"] = {
                "status": "decided",
                "session_id": "preparer-fixture",
                "evidence_paths": {p.name: str(p) for p in [source, package]},
                "evidence_hashes": {p.name: sha(p.read_text()) for p in [source, package]},
            }
            assignments.save(state)
            arguments.update(
                action="assign_prepared",
                package_path=str(package),
                package_sha256=sha(package.read_text()),
                module_path=str(source),
                module_sha256=sha(source.read_text()),
            )
        assigned[role] = assignments.dispatch(req, arguments)
        if role == "policy":
            # The first process cannot finish until the other reaches the provider.
            # The real Host reports it active; promotion must not orphan it.
            with pytest.raises(ValueError, match="Settle existing workers"):
                assignments.guard_gateway(
                    req, "POST", "/evolution/trials/synthetic-successor/decision", {"retain": True}
                )
    for role, item in assigned.items():
        session = item["session_id"]
        record = sandbox.records[session]
        try:
            code = record["process"].wait(timeout=25)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "Worker did not exit: " + (record["directory"] / "process-output.log").read_text()
            )
        assert code == 0, (record["directory"] / "process-output.log").read_text()
        files = sandbox.launches[session]
        initial = json.loads(files["checkpoint.json"])
        assert len(initial) == 1 and OLD_MAIN not in initial[0]["content"]
        assert "checkpoint.json" not in json.loads(files["worker-task.json"])["source_hashes"]
        for name, digest in json.loads(files["worker-task.json"])["source_hashes"].items():
            assert sha(files[name]) == digest
        checkpoint = json.loads((record["directory"] / "checkpoint.json").read_text())
        outputs = {
            entry["call_id"]: json.loads(entry["output"])
            for entry in checkpoint
            if entry.get("type") == "function_call_output"
        }
        assert outputs[role + "0"]["http_error"] == 403
        assert outputs[role + "1"] == "denied"
        assert outputs[role + "2"]["status"] == "completed"
        assert outputs[role + "3"]["status"] == "ready"
        assert outputs[role + "4"]["recipient"] == "main"
        assert outputs[role + "4"]["access"] == "read_only"
        assert outputs[role + "5"] == outputs[role + "6"] == "written"
        assert (
            "--worker-task"
            in json.loads((record["directory"] / "resume.json").read_text())["command"]
        )
        assert OLD_MAIN not in (record["directory"] / "history.jsonl").read_text()
        collected = assignments.dispatch(req, {"action": "collect", "id": role + "-fixture"})
        assert collected["status"] == "awaiting_decision"
        assert collected["evidence_hashes"]["research/result.json"] == sha(
            (record["directory"] / "research/result.json").read_text()
        )
        decided = assignments.dispatch(
            req,
            {
                "action": "decide",
                "id": role + "-fixture",
                "decision": "adopt",
                "reason": "Synthetic evidence matches the bounded fixture, not scientific acceptance",
            },
        )
        assert decided["status"] == "decided"
    assert {entry["role"] for entry in provider_calls} == {"policy", "harness"}
    assert len(remote_calls) == 2  # Delivery repair must not replay research jobs.
    assert len(provider_calls) == (4 if any(p["repair"] for p in provider_calls) else 2)
    assert not bridge_errors
    assert not (tmp_path / "outside-worker.txt").exists()
    assert all(
        "MAIN-PHASE-NOT-AUTOMATIC-WORKER-CONTEXT" not in entry["request"]["instructions"]
        for entry in provider_calls
    )
    with pytest.raises(PermissionError, match="delegate"):
        assignments.guard_gateway(req, "POST", "/jobs", {})
    sessions = [item["session_id"] for item in assigned.values()]
    denied = client.get(
        "/sessions/" + sessions[1] + "/artifacts",
        params={"path": "worker-result.json"},
        headers={"Authorization": "Bearer " + sandbox.tokens[sessions[0]]},
    )
    assert denied.status_code == 403

    # The real Host returns bytes, but the main refuses an altered evidence file.
    policy = sandbox.records[assigned["policy"]["session_id"]]["directory"]
    (policy / "research/result.json").write_text("tampered synthetic evidence")
    with pytest.raises(ValueError, match="hash mismatch"):
        assignments.dispatch(req, {"action": "collect", "id": "policy-fixture"})
    assert assignments.load()["assignments"]["policy-fixture"]["status"] == "decided"
