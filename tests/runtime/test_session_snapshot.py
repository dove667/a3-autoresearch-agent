import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app


def test_status_snapshot_cannot_miss_a_concurrently_created_successor(tmp_path):
    inspecting, created = threading.Event(), threading.Event()

    class Backend:
        def __init__(self):
            self.tokens = {}

        def create(self, owner, command, files, token):
            self.tokens[owner] = token
            return {"session_id": owner, "status": "created"}

        def status(self, owner, session_id):
            if session_id == parent:
                inspecting.set()
                # The successor cannot register until this consistent snapshot is released.
                finished = created.wait(0.1)
                return {"status": "exited" if finished else "running", "exit_code": 0}
            return {"status": "running", "exit_code": 0}

    backend = Backend()
    client = TestClient(
        create_app(
            model_name="controlled/model",
            sessions={"root-token": "root"},
            forward_model=lambda **kwargs: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            sandbox=backend,
        )
    )
    root_headers = {"Authorization": "Bearer root-token"}
    parent = host_create_session(client, headers=root_headers, json={"command": ["true"]}).json()[
        "session_id"
    ]

    def create_successor():
        assert inspecting.wait(3)
        response = host_create_session(
            client,
            headers={"Authorization": "Bearer " + backend.tokens[parent]},
            json={"command": ["true"]},
        )
        response.raise_for_status()
        created.set()
        return response.json()["session_id"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(create_successor)
        snapshot = client.get("/sessions?include_status=true", headers=root_headers)
        snapshot.raise_for_status()
        records = snapshot.json()["sessions"]
        assert len(records) == 1
        assert records[0]["status"] == "running"
        child = pending.result(timeout=3)
    records = client.get("/sessions?include_status=true", headers=root_headers).json()["sessions"]
    assert {record["session_id"] for record in records} == {parent, child}
    assert any(record["status"] == "running" for record in records)
