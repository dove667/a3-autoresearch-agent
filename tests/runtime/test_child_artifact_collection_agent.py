"""Actual Agent tools + production Host ACL; scripted model, no paid/remote work."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from test_orchestration_pipeline import ProcessSandbox

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.versions import CandidateVersions


def test_actual_agent_supplements_delivery_from_own_worker_child(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    sandbox = ProcessSandbox(tmp_path / "processes", versions)
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    raw = ("# UTF8 child-produced evidence 数据\n" * 2000).encode()
    source_path = "research/module.py"
    calls, errors = [], []
    source_id = None
    sibling_id = None

    def tool(name, call_id, arguments):
        return {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        }

    def provider(**request):
        calls.append(request)
        assert len(calls) == 1  # --test exits after the real ordered tool round.
        schema = next(x for x in request["tools"] if x.get("name") == "delegate")
        assert "collect_artifacts" in schema["parameters"]["properties"]["action"]["enum"]
        return {
            "status": "completed",
            "output": [
                tool("delegate", "lookup-missing", {"action": "list", "id": "never-created"}),
                tool(
                    "delegate", "reconcile-missing", {"action": "reconcile", "id": "never-created"}
                ),
                tool("delegate", "collect-original", {"action": "collect", "id": "prep"}),
                tool(
                    "delegate",
                    "reject-sibling",
                    {
                        "action": "collect_artifacts",
                        "id": "prep",
                        "source_session_id": sibling_id,
                        "artifacts": [{"path": source_path, "sha256": sha(raw)}],
                    },
                ),
                tool(
                    "delegate",
                    "decide-original",
                    {
                        "action": "decide",
                        "id": "prep",
                        "decision": "follow_up",
                        "reason": "Fixture original report is partial; preserve it.",
                    },
                ),
                tool(
                    "delegate",
                    "supplement",
                    {
                        "action": "collect_artifacts",
                        "id": "prep",
                        "source_session_id": source_id,
                        "artifacts": [{"path": source_path, "sha256": sha(raw)}],
                    },
                ),
                tool(
                    "read",
                    "read-supplement",
                    {
                        "path": f"orchestration/results/prep/child-artifacts/{source_id}/{source_path}",
                        "limit": 100,
                    },
                ),
            ],
        }

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
        remote_call=lambda *args: (_ for _ in ()).throw(AssertionError("No remote work")),
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:

        class Bridge(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def log_message(self, *args):
                pass

            def forward(self):
                try:
                    data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    response = client.request(
                        self.command,
                        self.path,
                        content=data or None,
                        headers={
                            "Authorization": self.headers.get("Authorization", ""),
                            "Content-Type": "application/json",
                        },
                    )
                    self.send_response(response.status_code)
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception as error:  # noqa: BLE001 — record test bridge failures
                    errors.append(type(error).__name__)
                    self.send_response(500)
                    self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sandbox.endpoint = "http://127.0.0.1:" + str(server.server_port)
        try:
            task = {"id": "prep", "role": "harness", "coordinator_session_id": "main"}
            report = {
                "id": "prep",
                "role": "harness",
                "status": "blocked",
                "summary": "Fixture child output not declared in this original report.",
                "evidence": [],
                "exports": [],
            }
            response = client.post(
                "/sessions",
                headers={"Authorization": "Bearer main-token"},
                json={
                    "command": ["python", "-c", "pass"],
                    "files": {
                        "worker-task.json": json.dumps(task),
                        "worker-result.json": json.dumps(report),
                    },
                },
            )
            assert response.status_code == 200, response.text
            producer = response.json()["session_id"]
            assert sandbox.records[producer]["process"].wait(timeout=10) == 0
            response = client.post(
                "/sessions",
                headers={"Authorization": "Bearer " + sandbox.tokens[producer]},
                json={"command": ["python", "-c", "pass"], "files": {source_path: raw.decode()}},
            )
            assert response.status_code == 200, response.text
            source_id = response.json()["session_id"]
            assert sandbox.records[source_id]["process"].wait(timeout=10) == 0
            # Existing Host boundary: producer can read its child, child cannot read producer.
            assert (
                client.get(
                    f"/sessions/{producer}/artifacts?path=worker-task.json",
                    headers={"Authorization": "Bearer " + sandbox.tokens[source_id]},
                ).status_code
                == 403
            )
            sibling = client.post(
                "/sessions",
                headers={"Authorization": "Bearer main-token"},
                json={"command": ["python", "-c", "pass"], "files": {source_path: raw.decode()}},
            )
            assert sibling.status_code == 200
            sibling_id = sibling.json()["session_id"]
            assert sandbox.records[sibling_id]["process"].wait(timeout=10) == 0
            from a3_research_agent.packaging.harness_payload import build_harness_payload

            files = build_harness_payload()
            files.update(
                {
                    "web_tools.py": "TOOLS=[]\nROUTES={}\n",
                    "skill_tools.py": "TOOLS=[]\nROUTES={}\n",
                    "checkpoint.json": '[{"role":"user","content":"Synthetic supplemental collection fixture"}]',
                    "orchestration/assignments.json": json.dumps(
                        {
                            "version": 1,
                            "assignments": {
                                "prep": {
                                    "id": "prep",
                                    "role": "harness",
                                    "status": "assigned",
                                    "session_id": producer,
                                    "deliverables": [],
                                    "task_sha256": sha(
                                        json.dumps(
                                            task, sort_keys=True, ensure_ascii=False
                                        ).encode()
                                    ),
                                }
                            },
                        }
                    ),
                }
            )
            version = versions.create("main", files)["version_id"]
            sandbox.create_version(
                "main",
                ["python", "-m", "a3_research_agent.harness.agent", "--resume", "--test"],
                "main-token",
                version,
            )
            record = sandbox.records["main"]
            assert record["process"].wait(timeout=20) == 0, (
                record["directory"] / "process-output.log"
            ).read_text()
            checkpoint = json.loads((record["directory"] / "checkpoint.json").read_text())
            results = {
                x["call_id"]: json.loads(x["output"])
                for x in checkpoint
                if x.get("type") == "function_call_output"
            }
            assert results["lookup-missing"]["found"] is False
            assert results["reconcile-missing"]["found"] is False
            assert results["reconcile-missing"]["retry_safe"] is None
            assert results["collect-original"]["result"] == report
            assert results["reject-sibling"]["error"] == "tool_execution_failed"
            assert results["decide-original"]["decision"] == "follow_up"
            receipt = results["supplement"]
            assert (
                receipt["source_session"] == source_id and receipt["producer_session"] == producer
            )
            assert receipt["generation_proven"] is False
            target = record["directory"] / receipt["evidence_paths"][source_path]
            assert target.read_bytes() == raw
            assert results["read-supplement"]["sha256"] == sha(raw)
            ledger = json.loads(
                (record["directory"] / "orchestration/assignments.json").read_text()
            )
            assert ledger["assignments"]["prep"]["result"] == report
            assert len(ledger["assignments"]["prep"]["artifact_collections"]) == 1
            assert not errors
            assert len(sandbox.records) == 4  # Collection never dispatches another process.
        finally:
            sandbox.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
