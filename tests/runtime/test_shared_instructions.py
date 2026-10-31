import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jinja2 import StrictUndefined, Template

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app
from runtime.local.launch import freeze_inputs


class Backend:
    def __init__(self):
        self.tokens = {}

    def create_version(self, owner, command, token, version):
        self.tokens[owner] = token
        return {"session_id": owner, "version_id": version}

    def status(self, owner, session):
        return {"status": "running"}

    def artifact(self, owner, session, path):
        return {"data": "evidence"}


def test_common_rules_follow_main_and_trial_roles_without_affecting_workers(tmp_path):
    calls = []

    def forward(**kwargs):
        calls.append(deepcopy(kwargs))
        return {"output": []}

    backend = Backend()
    versions = CandidateVersions(tmp_path / "versions")
    baseline = versions.create("root", {"entry.py": "pass"})["version_id"]
    app = create_app(
        model_name="test/model",
        sessions={"root-token": "root"},
        main_owner="root",
        forward_model=forward,
        remote_call=lambda *args: {},
        sandbox=backend,
        versions=versions,
        usage_log=tmp_path / "usage.jsonl",
        common_instructions="FROZEN COMMON",
    )
    client = TestClient(app)
    root = {"Authorization": "Bearer root-token"}
    candidate = client.post(
        "/evolution/trials",
        headers=root,
        json={
            "baseline_version": baseline,
            "checklist": "test change",
            "command": ["true"],
            "files": {"entry.py": "pass # candidate"},
        },
    ).json()["session_id"]
    worker = client.post(
        "/sessions", headers=root, json={"command": ["true"], "files": {"probe.py": "pass"}}
    ).json()["session_id"]
    context = [{"role": "system", "content": "Own system"}, {"role": "user", "content": "Own task"}]
    tools = [{"type": "function", "name": "files"}]

    def query(token):
        response = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + token},
            json={"input": context, "tools": tools, "instructions": "Own role"},
        )
        assert response.status_code == 200
        assert calls[-1]["input"] == context
        assert calls[-1]["tools"] == tools
        return calls[-1]["instructions"]

    assert query("root-token") == "FROZEN COMMON\n\nOwn role"
    assert query(backend.tokens[candidate]) == "FROZEN COMMON\n\nOwn role"
    assert query(backend.tokens[worker]) == "Own role"
    promoted = client.post(
        f"/evolution/trials/{candidate}/decision",
        headers=root,
        json={"retain": True, "reason": "tested", "evidence": ["result.json"]},
    )
    assert promoted.status_code == 200
    assert query(backend.tokens[candidate]) == "FROZEN COMMON\n\nOwn role"
    assert query("root-token") == "Own role"  # Retired main receives no new common layer.
    assert query(backend.tokens[worker]) == "Own role"


def test_initial_system_template_includes_the_same_common_rules():
    config = yaml.safe_load(Path("configs/rsi-bootstrap.yaml").read_text())
    rendered = Template(config["agent"]["system_template"], undefined=StrictUndefined).render(
        common_instructions="FROZEN COMMON"
    )
    assert "FROZEN COMMON" in rendered


def test_resume_freezes_common_rules_and_rejects_tampering(tmp_path):
    parent = tmp_path / "old"
    (parent / "inputs").mkdir(parents=True)
    names = ("goal.md", "acceptance.md", "bootstrap.yaml", "execution.md", "common-instructions.md")
    hashes = {}
    for name in names:
        text = "original " + name
        (parent / "inputs" / name).write_text(text)
        hashes[name] = hashlib.sha256(text.encode()).hexdigest()
    (parent / "startup.json").write_text(json.dumps({"inputs": hashes}))
    target = tmp_path / "new"
    target.mkdir()
    freeze_inputs(target, parent)
    assert (target / "common-instructions.md").read_text() == "original common-instructions.md"
    (parent / "inputs/common-instructions.md").write_text("modified")
    with pytest.raises(ValueError, match="hash mismatch"):
        freeze_inputs(target, parent)
