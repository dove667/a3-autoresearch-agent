"""Single research identity, with one bounded successor and parallel remote jobs."""

from fastapi.testclient import TestClient
from test_evolution import begin, setup

from a3_research_agent.host.gateway.app import create_app


def test_agent_cannot_create_workers_even_by_claiming_validation_purpose(tmp_path):
    client, backend, baseline = setup(tmp_path)
    successor = begin(client, baseline)
    headers = {"Authorization": "Bearer " + backend.tokens[successor]}
    before = set(backend.records)
    for purpose in ("worker", "candidate_validation", "successor"):
        response = client.post(
            "/sessions",
            headers=headers,
            json={
                "command": ["python", "entry.py"],
                "files": {"entry.py": "pass"},
                "purpose": purpose,
            },
        )
        assert response.status_code in {403, 422}
    assert set(backend.records) == before


def test_main_submits_independent_jobs_without_worker_handoff(tmp_path):
    requests = []

    def remote(action, payload):
        requests.append((action, payload))
        return {"job_id": str(len(requests)), "status": "queued"}

    app = create_app(
        model_name="test/model",
        sessions={"token": "researcher"},
        main_owner="researcher",
        forward_model=lambda **kwargs: {},
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:
        for name in ("a", "b", "c"):
            response = client.post(
                "/jobs",
                headers={"Authorization": "Bearer token"},
                json={"command": ["python", name + ".py"], "gpu": False},
            )
            assert response.status_code == 200
    assert [action for action, _ in requests] == ["submit"] * 3


def test_predecessor_cannot_submit_research_while_successor_is_testing(tmp_path):
    client, _backend, baseline = setup(tmp_path)
    begin(client, baseline)
    response = client.post(
        "/jobs",
        headers={"Authorization": "Bearer root-token"},
        json={"command": ["true"], "gpu": False},
    )
    assert response.status_code == 409


def test_same_run_reads_are_shared_but_job_management_follows_adoption(tmp_path):
    client, backend, baseline = setup(tmp_path)
    gateway = next(
        route.endpoint.__self__
        for route in client.app.routes
        if getattr(route, "path", None) == "/jobs" and "POST" in route.methods
    )
    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        if payload.get("job_id") == "old-job" and payload["experiment_id"] != "root":
            return {"status": "error", "error": "PermissionError"}
        return {"status": "running", "area": payload.get("area", "workspace")}

    gateway.remote_call = remote
    gateway.jobs["root"] = {"old-job"}
    successor = begin(client, baseline)
    headers = {"Authorization": "Bearer " + backend.tokens[successor]}
    assert client.get("/jobs/old-job", headers=headers).status_code == 200
    assert client.delete("/jobs/old-job", headers=headers).status_code == 403
    backend.records[successor]["report"] = True
    retained = client.post(
        f"/evolution/trials/{successor}/decision",
        headers={"Authorization": "Bearer root-token"},
        json={"retain": True, "reason": "fixture", "evidence": ["result.json"]},
    )
    assert retained.status_code == 200
    adoption_calls = len(calls)
    assert client.get("/jobs/old-job", headers=headers).status_code == 200
    assert (
        client.get("/jobs/old-job/artifacts?path=result.json", headers=headers).status_code == 200
    )
    assert client.delete("/jobs/old-job", headers=headers).status_code == 200
    assert all(payload["experiment_id"] == "root" for _, payload in calls[adoption_calls:])
    assert gateway.job_owner(successor, "unrelated-job") == successor
    assert gateway.job_owner("unrelated-agent", "old-job") == "unrelated-agent"


def test_real_single_agent_submits_two_jobs_before_waiting(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from process_sandbox import ProcessSandbox

    from a3_research_agent.host.sessions.versions import CandidateVersions
    from a3_research_agent.packaging.harness_payload import build_harness_payload

    versions = CandidateVersions(tmp_path / "versions")
    sandbox = ProcessSandbox(tmp_path / "processes", versions)
    submitted = []
    decisions = []

    def provider(**request):
        decisions.append(request)
        assert "delegate" not in {tool["name"] for tool in request["tools"]}
        assert "research yourself" in request["instructions"]
        return {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "gateway",
                    "call_id": name,
                    "arguments": json.dumps(
                        {
                            "method": "POST",
                            "path": "/jobs",
                            "body": {"command": ["python", name + ".py"]},
                        }
                    ),
                }
                for name in ("collect", "evaluate")
            ],
        }

    def remote(operation, payload):
        assert operation == "submit", "No wait/status between independent submissions"
        submitted.append(payload)
        return {"job_id": str(len(submitted)), "status": "queued"}

    app = create_app(
        model_name="fixture/scripted",
        sessions={"root-token": "root"},
        main_owner="root",
        start_main_session=True,
        sandbox=sandbox,
        versions=versions,
        forward_model=provider,
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:

        class Bridge(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                response = client.request(
                    self.command,
                    self.path,
                    content=body or None,
                    headers={
                        "Authorization": self.headers.get("Authorization", ""),
                        "Content-Type": "application/json",
                    },
                )
                self.send_response(response.status_code)
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sandbox.endpoint = "http://127.0.0.1:" + str(server.server_port)
        files = build_harness_payload()
        files["task.txt"] = "Submit two independent synthetic remote jobs."
        try:
            session = app.state.start_local_harness(
                ["python", "-m", "a3_research_agent.harness.agent", "--test"],
                files,
                "Bearer root-token",
            )["session_id"]
            record = sandbox.records[session]
            assert record["process"].wait(timeout=20) == 0
            assert len(decisions) == 1
            assert len(submitted) == 2
            assert {item["experiment_id"] for item in submitted} == {session}
            assert len(sandbox.records) == 1
            assert not (record["directory"] / "orchestration").exists()
        finally:
            sandbox.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
