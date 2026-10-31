import json
import sys
import threading

from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.input_versions import INPUT_SOURCES, InputVersions


def setup(tmp_path):
    source = tmp_path / "source"
    for name, relative in INPUT_SOURCES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Version: 0.1.0\nold {name}\n")
        if name == "bootstrap.yaml":
            path.write_text(
                'version: 0.1.0\nagent:\n  system_template: "{{ common_instructions }}"\n  instance_template: "{{ task }}"\nrun:\n  task: task\n'
            )
    versions = InputVersions(source, tmp_path / "run")
    versions.activate(versions.prepare(), "start")
    calls = []
    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"agent-token": "main"},
            main_owner="main",
            forward_model=lambda **kwargs: calls.append(kwargs) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "run/model-usage.jsonl",
            input_versions=versions,
            operator_token="operator-secret",
        )
    )
    return client, versions, calls


def test_hot_reload_requires_operator_and_confirmation_of_exact_preview(tmp_path):
    client, versions, calls = setup(tmp_path)
    agent = {"Authorization": "Bearer agent-token"}
    operator = {"Authorization": "Bearer operator-secret"}
    assert client.get("/control/inputs", headers=agent).status_code == 403
    assert client.get("/control/inputs").status_code == 403
    path = versions.source_root / INPUT_SOURCES["common-instructions.md"]
    path.write_text("Version: 0.1.1\nnew common prompt\n")
    preview = client.get("/control/inputs", headers=operator).json()
    digest = preview["proposed"]["digest"]
    assert preview["warning"]
    assert (
        client.post("/control/inputs", headers=operator, json={"confirm": "0" * 64}).status_code
        == 409
    )
    assert (
        client.post("/control/inputs", headers=agent, json={"confirm": digest}).status_code == 403
    )
    assert "new common prompt" not in versions.model_instructions()[1]
    response = client.post("/control/inputs", headers=operator, json={"confirm": digest})
    assert response.status_code == 200
    context = [{"role": "user", "content": "saved context"}]
    result = client.post("/v1/responses", headers=agent, json={"input": context})
    assert result.status_code == 200
    assert calls[-1]["input"] == context
    assert "new common prompt" in calls[-1]["instructions"]
    assert result.headers["X-A3-Input-Revision"] == versions.manifest["revision"]
    usage = json.loads((versions.run / "model-usage.jsonl").read_text().splitlines()[-1])
    assert usage["input_revision"] == versions.manifest["revision"]
    assert usage["input_versions"]["common-instructions.md"]["version"] == "0.1.1"


def test_changed_source_after_preview_invalidates_confirmation(tmp_path):
    client, versions, _ = setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    preview = client.get("/control/inputs", headers=operator).json()
    (versions.source_root / INPUT_SOURCES["goal.md"]).write_text("Version: 0.1.1\nnew goal\n")
    result = client.post(
        "/control/inputs", headers=operator, json={"confirm": preview["proposed"]["digest"]}
    )
    assert result.status_code == 409
    assert "new goal" not in versions.model_instructions()[1]


def test_inflight_model_response_keeps_its_version_while_next_call_uses_update(tmp_path):
    _, versions, _ = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    received = []

    def forward(**kwargs):
        received.append(kwargs["instructions"])
        if len(received) == 1:
            entered.set()
            assert release.wait(5)
        return {"output": []}

    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"agent-token": "main"},
            main_owner="main",
            forward_model=forward,
            remote_call=lambda *args: {},
            usage_log=versions.run / "model-usage.jsonl",
            input_versions=versions,
            operator_token="operator-secret",
        )
    )
    agent = {"Authorization": "Bearer agent-token"}
    responses = []
    thread = threading.Thread(
        target=lambda: responses.append(
            client.post("/v1/responses", headers=agent, json={"input": "work"})
        )
    )
    thread.start()
    assert entered.wait(5)
    before = versions.manifest["revision"]
    try:
        (versions.source_root / INPUT_SOURCES["goal.md"]).write_text(
            "Version: 0.1.1\nupdated goal\n"
        )
        operator = {"Authorization": "Bearer operator-secret"}
        preview = client.get("/control/inputs", headers=operator).json()
        assert (
            client.post(
                "/control/inputs", headers=operator, json={"confirm": preview["proposed"]["digest"]}
            ).status_code
            == 200
        )
    finally:
        release.set()
        thread.join(5)
    assert responses[0].headers["X-A3-Input-Revision"] == before
    after = client.post("/v1/responses", headers=agent, json={"input": "work"})
    assert after.headers["X-A3-Input-Revision"] != before
    assert "updated goal" not in received[0]
    assert "updated goal" in received[1]


def test_cli_preview_warns_without_loading_and_confirmation_applies(tmp_path, monkeypatch, capsys):
    from runtime.local import reload_inputs
    from runtime.local.run_state import RunStateStore

    client, versions, _ = setup(tmp_path)
    RunStateStore(versions.run).save(
        {"endpoint": "http://host.docker.internal:1234", "operator_token": "operator-secret"}
    )
    (versions.source_root / INPUT_SOURCES["goal.md"]).write_text("Version: 0.1.1\nnew goal\n")

    def connect(**kwargs):
        client.headers.update(kwargs["headers"])
        return client

    monkeypatch.setattr(reload_inputs.httpx, "Client", connect)
    monkeypatch.setattr(sys, "argv", ["reload_inputs", str(versions.run)])
    before = versions.manifest["revision"]
    reload_inputs.main()
    text = capsys.readouterr().out
    assert "警告" in text and "仅预览" in text and "operator-secret" not in text
    assert versions.manifest["revision"] == before
    digest = versions.prepare()["digest"]
    monkeypatch.setattr(sys, "argv", ["reload_inputs", str(versions.run), "--confirm", digest])
    reload_inputs.main()
    assert versions.manifest["revision"] != before


def test_host_finishing_rejects_late_hot_reload(tmp_path):
    client, versions, _ = setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    digest = client.get("/control/inputs", headers=operator).json()["proposed"]["digest"]
    before = versions.manifest["revision"]
    client.app.state.begin_shutdown()
    response = client.post("/control/inputs", headers=operator, json={"confirm": digest})
    assert response.status_code == 503
    assert versions.manifest["revision"] == before
