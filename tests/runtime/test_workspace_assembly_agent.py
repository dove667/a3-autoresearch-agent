"""Real Harness tools submit identifiers/references; Host, not fixture, composes files.

Provider decisions are scripted. Processes emulate workspace roots; real Docker validation
is recorded separately. This is not proof of autonomous real-model planning.
"""

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from test_orchestration_pipeline import ProcessSandbox

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app


def test_agent_tools_assemble_and_execute_without_a_files_payload(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    sandbox = ProcessSandbox(tmp_path / "processes", versions)
    submitted, errors, calls = [], [], []
    runner = "import pathlib\nfrom dependency import VALUE\npathlib.Path('research/out.txt').write_text(str(VALUE))\n"
    # Running a script in research/ needs its parent module search path explicitly.
    runner = "import sys\nsys.path.insert(0,'.')\n" + runner

    def tool(name, key, args):
        return {
            "type": "function_call",
            "name": name,
            "call_id": key,
            "arguments": json.dumps(args),
        }

    def provider(**request):
        calls.append(True)
        assert len(calls) <= 16
        results = {
            x["call_id"]: json.loads(x["output"])
            for x in request["input"]
            if x.get("type") == "function_call_output"
        }
        if "w" not in results:
            out = [
                tool("write", "w", {"path": "research/runner.py", "content": runner}),
                tool("gateway", "v", {"method": "GET", "path": "/versions"}),
            ]
        elif "r" not in results:
            out = [tool("read", "r", {"path": "research/runner.py"})]
        elif "s" not in results:
            body = {
                "command": ["python", "research/runner.py"],
                "version_id": results["v"]["versions"][0]["version_id"],
                "workspace_files": [
                    {"path": "research/runner.py", "sha256": results["r"]["sha256"]}
                ],
            }
            submitted.append(body)
            out = [tool("gateway", "s", {"method": "POST", "path": "/sessions", "body": body})]
        else:
            child = results["s"]["session_id"]
            if "a" not in results:
                # The scripted test provider waits for its own child, not a remote job/model.
                record = sandbox.records[child]
                assert record["process"].wait(timeout=10) == 0
                out = [
                    tool(
                        "gateway",
                        "a",
                        {
                            "method": "GET",
                            "path": f"/sessions/{child}/artifacts?path=research/out.txt",
                        },
                    )
                ]
            else:
                assert base64.b64decode(results["a"]["data"]) == b"7"
                report = {
                    "id": "assembly",
                    "role": "harness",
                    "status": "complete",
                    "summary": "Synthetic result was read through authorized artifact API",
                    "evidence": [],
                    "exports": [],
                }
                out = [
                    tool(
                        "write",
                        "done",
                        {"path": "worker-result.json", "content": json.dumps(report)},
                    )
                ]
        return {"status": "completed", "model": "fixture/scripted", "output": out}

    restored = {
        "sessions": {"root-token": "root"},
        "root_owners": ["root"],
        "children": {},
        "scope_parents": {},
        "jobs": {},
    }
    app = create_app(
        model_name="fixture/scripted",
        sessions={},
        sandbox=sandbox,
        versions=versions,
        restored_state=restored,
        forward_model=provider,
        remote_call=lambda *a: (_ for _ in ()).throw(AssertionError("No remote calls")),
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:

        class Bridge(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception as error:  # noqa: BLE001 - record fixture bridge failures
                    errors.append(type(error).__name__)
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sandbox.endpoint = "http://127.0.0.1:" + str(server.server_port)
        try:
            from a3_research_agent.packaging.harness_payload import build_harness_payload

            files = build_harness_payload()
            files.update(
                {
                    "web_tools.py": "TOOLS=[]\nROUTES={}\n",
                    "skill_tools.py": "TOOLS=[]\nROUTES={}\n",
                    "dependency.py": "VALUE=7\n",
                }
            )
            task = {
                "version": 1,
                "assignment_nonce": "a" * 32,
                "id": "assembly",
                "role": "harness",
                "deliverables": [],
                "objective": "Synthetic assembly fixture",
                "stop_condition": "Read result then stop",
                "coordinator_session_id": "root",
                "source_hashes": {
                    n: hashlib.sha256(v.encode()).hexdigest() for n, v in files.items()
                },
            }
            files["worker-task.json"] = json.dumps(task)
            files["checkpoint.json"] = '[{"role":"user","content":"Synthetic bounded assembly"}]'
            response = client.post(
                "/sessions",
                headers={"Authorization": "Bearer root-token"},
                json={
                    "command": [
                        "python",
                        "agent.py",
                        "--resume",
                        "--worker-task",
                        "worker-task.json",
                    ],
                    "files": files,
                },
            )
            assert response.status_code == 200, response.text
            parent = response.json()["session_id"]
            record = sandbox.records[parent]
            assert record["process"].wait(timeout=20) == 0, (
                record["directory"] / "process-output.log"
            ).read_text()
            assert (
                json.loads((record["directory"] / "worker-result.json").read_text())["status"]
                == "complete"
            )
            assert len(submitted) == 1 and set(submitted[0]) == {
                "command",
                "version_id",
                "workspace_files",
            }
            assert not errors
            child = next(key for key in sandbox.records if key != parent)
            assert (
                sandbox.records[child]["directory"] / "dependency.py"
            ).read_text() == "VALUE=7\n"
            assert (sandbox.records[child]["directory"] / "research/out.txt").read_text() == "7"
            assert "research/runner.py" not in versions.read_snapshot(
                versions.root, submitted[0]["version_id"]
            )
        finally:
            sandbox.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
