"""Research operations map typed arguments to fixed authorized Host routes."""

import hashlib

from a3_research_agent.harness import file_tools, host_tools
from a3_research_agent.host.sessions.artifact_pages import read_session_page

JOB = "a" * 32
BASE = "b" * 32
EXPORT = "export-" + "c" * 32


def request_log():
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"recorded": True}

    return calls, request


def test_fixed_export_routes_and_default_outputs():
    calls, request = request_log()
    assert host_tools.dispatch("export_create", {"job_id": JOB, "path": "checkpoint.pt"}, request)[
        "recorded"
    ]
    host_tools.dispatch("export_manifest", {"export_id": EXPORT}, request)
    host_tools.dispatch(
        "export_share", {"export_id": EXPORT, "session_id": "session-other"}, request
    )
    host_tools.dispatch(
        "export_revoke", {"export_id": EXPORT, "session_id": "session-other"}, request
    )
    assert calls == [
        ("POST", f"/jobs/{JOB}/exports", {"path": "checkpoint.pt", "location": "outputs"}),
        ("GET", f"/jobs/exports/{EXPORT}/manifest", None),
        ("POST", f"/jobs/exports/{EXPORT}/grants", {"session_id": "session-other"}),
        ("DELETE", f"/jobs/exports/{EXPORT}/grants/session-other", None),
    ]
    assert (
        host_tools.dispatch("export_delete", {"export_id": "../secrets"}, request)["error"]
        == "invalid_export_id"
    )
    assert len(calls) == 4


def test_visual_and_comparison_tools_do_not_accept_raw_transport_parameters():
    calls, request = request_log()
    frame = {
        "job_id": JOB,
        "path": "frames/front.png",
        "episode_id": "seed1",
        "step": 5,
        "sim_time_s": 0.25,
        "camera": "front",
    }
    result = host_tools.dispatch(
        "vision_inspect", {"question": "Where does placement diverge?", "frames": [frame]}, request
    )
    assert result["recorded"] and calls[-1][2]["frames"][0]["area"] == "outputs"
    assert (
        host_tools.dispatch(
            "vision_inspect",
            {"question": "q", "frames": [{**frame, "sim_time_s": float("nan")}]},
            request,
        )["error"]
        == "invalid_sim_time_s"
    )
    assert (
        host_tools.dispatch("gpu_status", {"path": "/control/inputs"}, request)["error"]
        == "invalid_arguments"
    )
    host_tools.dispatch(
        "evaluation_compare",
        {
            "candidate_job_id": JOB,
            "baseline_job_id": BASE,
            "baseline_manifest": "base.json",
            "candidate_manifest": "candidate.json",
        },
        request,
    )
    assert calls[-1] == (
        "POST",
        f"/jobs/{JOB}/evaluation",
        {
            "baseline_job_id": BASE,
            "baseline_manifest": "base.json",
            "candidate_manifest": "candidate.json",
        },
    )


def test_session_file_page_uses_character_contract_and_stop_reason():
    calls, request = request_log()
    host_tools.dispatch(
        "session_artifact",
        {
            "session_id": "candidate-old",
            "path": "conversation/history.jsonl",
            "offset": 3,
            "limit": 6,
        },
        request,
    )
    assert calls[-1] == (
        "GET",
        "/sessions/candidate-old/artifacts?path=conversation%2Fhistory.jsonl&offset=3&limit=6&text=true",
        None,
    )
    host_tools.dispatch("session_stop", {"reason": "Saved results and next route"}, request)
    assert calls[-1] == ("POST", "/sessions/self/stop", {"reason": "Saved results and next route"})
    assert (
        host_tools.dispatch("session_status", {"session_id": "self?auth=bad"}, request)["error"]
        == "invalid_session_id"
    )


def test_directory_pages_and_atomic_versioned_writes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "config.json"
    result = file_tools.dispatch(
        "write",
        {"path": "config.json", "content": '{"step": 5}'},
        workspace=workspace,
        readonly_roots=[],
    )
    assert result["status"] == "created"
    digest = result["sha256"]
    assert (
        file_tools.dispatch(
            "write",
            {"path": "config.json", "content": "lost"},
            workspace=workspace,
            readonly_roots=[],
        )["error"]
        == "revision_conflict"
    )
    assert path.read_text() == '{"step": 5}'
    changed = file_tools.dispatch(
        "write",
        {"path": "config.json", "content": '{"step": 6}', "expected_sha256": digest},
        workspace=workspace,
        readonly_roots=[],
    )
    assert changed["sha256"] != digest
    (workspace / "z.py").write_text("pass")
    page = file_tools.dispatch("list", {"limit": 1}, workspace=workspace, readonly_roots=[])
    assert page["entries"][0]["name"] == "config.json" and page["next_offset"] == 1
    next_page = file_tools.dispatch(
        "list", {"offset": page["next_offset"], "limit": 1}, workspace=workspace, readonly_roots=[]
    )
    assert next_page["entries"][0]["type"] == "file" and next_page["next_offset"] is None
    selected = file_tools.dispatch(
        "read", {"path": "config.json", "fields": ["step"]}, workspace=workspace, readonly_roots=[]
    )
    assert selected["fields"] == {"step": 6}


def test_readonly_roots_and_symlink_escapes_are_enforced(tmp_path):
    workspace, readonly = tmp_path / "workspace", tmp_path / "input"
    workspace.mkdir()
    readonly.mkdir()
    (readonly / "prompt.md").write_text("instructions")
    (workspace / "outside").symlink_to(readonly, target_is_directory=True)
    rejected = file_tools.dispatch(
        "write",
        {"path": "outside/prompt.md", "content": "changed"},
        workspace=workspace,
        readonly_roots=[readonly],
    )
    assert rejected["error"] == "path_not_accessible"
    assert (readonly / "prompt.md").read_text() == "instructions"
    assert (
        file_tools.dispatch(
            "read",
            {"path": str(readonly / "prompt.md"), "fields": ["x"]},
            workspace=workspace,
            readonly_roots=[readonly],
        )["error"]
        == "source_not_json"
    )


def test_session_text_paging_handles_unicode_boundaries_and_binary(tmp_path):
    text = "中" * 21846 + "🙂abc"
    path = tmp_path / "history.jsonl"
    path.write_text(text)
    page = read_session_page(tmp_path, path.name, 21845, 3)
    assert page["content"] == text[21845:21848] and page["total"] == len(text)
    assert page["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert page["next_offset"] == 21848
    (tmp_path / "binary.dat").write_bytes(b"abc\xff")
    assert read_session_page(tmp_path, "binary.dat")["encoding"] == "binary"
    tail = read_session_page(tmp_path, path.name, 10**6, 10)
    assert tail["content"] == "" and tail["offset"] == tail["total"] and tail["eof"]


def test_native_session_text_endpoint_checks_identity_before_file_access(tmp_path):
    from fastapi.testclient import TestClient
    from host_fixtures import host_create_session

    from a3_research_agent.host.gateway.app import create_app

    class Runtime:
        def check_capacity(self):
            pass

        def create(self, owner, command, files, token, *, inputs=None):
            self.token = token
            return {"session_id": owner}

        def artifact_text(self, owner, session, path, offset, limit):
            return read_session_page(tmp_path, path, offset, limit)

    runtime = Runtime()
    (tmp_path / "result.txt").write_text("中文abcdef")
    app = create_app(
        model_name="fixture",
        sessions={"root-token": "root"},
        session_runtime=runtime,
        main_owner="root",
        forward_model=lambda **kwargs: {},
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:
        session = host_create_session(
            client, headers={"Authorization": "Bearer root-token"}, json={"command": ["true"]}
        ).json()["session_id"]
        headers = {"Authorization": "Bearer " + runtime.token}
        url = f"/sessions/{session}/artifacts?text=true&path=result.txt&offset=1&limit=3"
        assert client.get(url).status_code == 401
        page = client.get(url, headers=headers).json()
        assert page["content"] == "文ab" and page["unit"] == "characters"
        assert client.get(url.replace(session, "unknown"), headers=headers).status_code == 403
