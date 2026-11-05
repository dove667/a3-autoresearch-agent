from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jinja2 import StrictUndefined, Template

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.sessions.versions import CandidateVersions


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


@pytest.mark.parametrize("phase", [None, "phase-one", "phase-two"])
def test_phase_rules_follow_main_and_trial_roles_without_affecting_workers(tmp_path, phase):
    calls = []

    def forward(**kwargs):
        calls.append(deepcopy(kwargs))
        return {"output": []}

    inputs = None
    expected = "FROZEN PHASE"
    if phase:
        inputs = InputVersions(Path.cwd(), tmp_path / "run", phase=phase)
        inputs.activate(inputs.prepare(), "start")
        expected = inputs.model_instructions()[1]
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
        phase_instructions=expected,
        input_versions=inputs,
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

    assert query("root-token") == expected + "\n\nOwn role"
    assert query(backend.tokens[candidate]) == expected + "\n\nOwn role"
    assert query(backend.tokens[worker]) == "Own role"
    promoted = client.post(
        f"/evolution/trials/{candidate}/decision",
        headers=root,
        json={"retain": True, "reason": "tested", "evidence": ["result.json"]},
    )
    assert promoted.status_code == 200
    assert query(backend.tokens[candidate]) == expected + "\n\nOwn role"
    assert query("root-token") == "Own role"  # Retired main receives no new phase layer.
    assert query(backend.tokens[worker]) == "Own role"


def test_initial_system_template_includes_the_selected_phase_rules():
    config = yaml.safe_load(Path("agent-inputs/shared/bootstrap.yaml").read_text())
    rendered = Template(config["agent"]["system_template"], undefined=StrictUndefined).render(
        phase_instructions="FROZEN PHASE"
    )
    assert "FROZEN PHASE" in rendered
