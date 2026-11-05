import json
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions, input_sources

INPUT_SOURCES = input_sources("phase-one")


def setup(tmp_path):
    source = tmp_path / "source"
    for name, relative in INPUT_SOURCES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Version: 0.1.0\nold {name}\n")
        if name == "bootstrap.yaml":
            path.write_text(
                'version: 0.1.0\nagent:\n  system_template: "{{ phase_instructions }}"\n  instance_template: "{{ task }}"\nrun:\n  task: task\n'
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
    path = versions.source_root / INPUT_SOURCES["phase-instructions.md"]
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
    assert usage["input_versions"]["phase-instructions.md"]["version"] == "0.1.1"


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
    from a3_research_agent.cli import reload_inputs
    from a3_research_agent.host.runs.state import RunStateStore

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


def custom_setup(tmp_path):
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    directory = tmp_path / "custom-original"
    for name, source in input_sources("phase-one").items():
        p = directory / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((root / source).read_bytes())
    (directory / "SOUL.md").write_text("Version: 1.0.0\nfixture baseline principle\n")
    versions = InputVersions(root, tmp_path / "custom-run", custom_dir=directory)
    versions.activate(versions.prepare(), "start")
    replacement = tmp_path / "replacement"
    shutil.copytree(directory, replacement)
    (replacement / "SOUL.md").write_text("Version: 1.0.1\nnew authorized collaboration principle\n")
    calls = []
    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"agent-token": "main"},
            main_owner="main",
            forward_model=lambda **kwargs: calls.append(kwargs) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=versions.run / "usage.jsonl",
            input_versions=versions,
            operator_token="operator-secret",
        )
    )
    return client, versions, replacement, calls


def test_custom_source_selection_is_explicit_operator_owned_and_keeps_history(tmp_path):
    import shutil

    client, versions, replacement, calls = custom_setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    agent = {"Authorization": "Bearer agent-token"}
    original = versions.manifest.copy()
    archive = versions.run / "input-revisions" / original["revision"]
    old = (archive / "SOUL.md").read_bytes()
    assert (
        client.get(
            "/control/inputs", headers=agent, params={"source_dir": str(replacement)}
        ).status_code
        == 403
    )
    preview = client.get(
        "/control/inputs", headers=operator, params={"source_dir": str(replacement)}
    )
    assert preview.status_code == 200
    digest = preview.json()["proposed"]["digest"]
    assert (
        client.post(
            "/control/inputs",
            headers=agent,
            json={"source_dir": str(replacement), "confirm": digest},
        ).status_code
        == 403
    )
    assert versions.manifest == original
    applied = client.post(
        "/control/inputs",
        headers=operator,
        json={"source_dir": str(replacement), "confirm": digest},
    )
    assert applied.status_code == 200
    assert (archive / "SOUL.md").read_bytes() == old
    client.post(
        "/v1/responses",
        headers=agent,
        json={"input": [{"role": "user", "content": "unchanged history"}]},
    )
    assert "new authorized collaboration principle" in calls[-1]["instructions"]
    assert calls[-1]["input"] == [{"role": "user", "content": "unchanged history"}]
    shutil.rmtree(replacement)
    restored = InputVersions(versions.source_root, versions.run)
    assert restored.prepare()["contents"]["SOUL.md"].startswith("Version: 1.0.1")


def test_custom_source_changed_after_preview_is_not_activated(tmp_path):
    client, versions, replacement, _ = custom_setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    before = versions.manifest.copy()
    preview = client.get(
        "/control/inputs", headers=operator, params={"source_dir": str(replacement)}
    ).json()
    (replacement / "SOUL.md").write_text("Version: 1.0.2\nchanged after preview\n")
    assert (
        client.post(
            "/control/inputs",
            headers=operator,
            json={"source_dir": str(replacement), "confirm": preview["proposed"]["digest"]},
        ).status_code
        == 409
    )
    assert versions.manifest == before


@pytest.mark.parametrize("damage", ["same-version", "extra-file", "symlink"])
def test_custom_source_selection_rejects_invalid_replacement(tmp_path, damage):
    client, versions, replacement, _ = custom_setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    before = versions.manifest.copy()
    if damage == "same-version":
        (replacement / "SOUL.md").write_text("Version: 1.0.0\nchanged without version\n")
    elif damage == "extra-file":
        (replacement / "unexpected.txt").write_text("outside current file set")
    else:
        (replacement / "SOUL.md").unlink()
        (replacement / "SOUL.md").symlink_to(versions.run / "inputs/SOUL.md")
    assert (
        client.get(
            "/control/inputs", headers=operator, params={"source_dir": str(replacement)}
        ).status_code
        == 422
    )
    assert versions.manifest == before


def test_preset_cannot_change_input_mode_via_directory_selector(tmp_path):
    client, _versions, _ = setup(tmp_path)
    assert (
        client.get(
            "/control/inputs",
            headers={"Authorization": "Bearer operator-secret"},
            params={"source_dir": str(tmp_path)},
        ).status_code
        == 422
    )


def test_custom_source_digest_binds_directory_even_for_identical_contents(tmp_path):
    import shutil

    client, versions, replacement, _ = custom_setup(tmp_path)
    operator = {"Authorization": "Bearer operator-secret"}
    preview = client.get(
        "/control/inputs", headers=operator, params={"source_dir": str(replacement)}
    ).json()
    other = tmp_path / "other-source"
    shutil.copytree(replacement, other)
    assert (
        client.post(
            "/control/inputs",
            headers=operator,
            json={"source_dir": str(other), "confirm": preview["proposed"]["digest"]},
        ).status_code
        == 409
    )
    assert versions.manifest["files"]["SOUL.md"]["version"] == "1.0.0"


def test_reload_cli_selects_custom_directory_without_replacing_history(
    tmp_path, monkeypatch, capsys
):
    from a3_research_agent.cli import reload_inputs
    from a3_research_agent.host.runs.state import RunStateStore

    client, versions, replacement, _ = custom_setup(tmp_path)
    RunStateStore(versions.run).save(
        {"endpoint": "http://host.docker.internal:1234", "operator_token": "operator-secret"}
    )

    def connect(**kwargs):
        client.headers.update(kwargs["headers"])
        return client

    monkeypatch.setattr(reload_inputs.httpx, "Client", connect)
    original = versions.manifest["revision"]
    args = ["reload_inputs", str(versions.run), "--agent-inputs-dir", str(replacement)]
    monkeypatch.setattr(sys, "argv", args)
    reload_inputs.main()
    assert versions.manifest["revision"] == original
    assert "operator-secret" not in capsys.readouterr().out
    digest = versions.prepare_directory(replacement)["digest"]
    monkeypatch.setattr(sys, "argv", args + ["--confirm", digest])
    reload_inputs.main()
    assert versions.manifest["files"]["SOUL.md"]["version"] == "1.0.1"
    assert (versions.run / "input-revisions" / original / "SOUL.md").is_file()
