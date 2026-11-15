"""Only the current research identity receives the selected task instructions."""

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from task_input_support import task_inputs

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("versioned", [False, True])
def test_only_current_main_receives_task_instructions(tmp_path, versioned):
    calls = []
    inputs, expected = None, "EXPLICIT TASK CONTRACT"
    if versioned:
        inputs = InputVersions(ROOT, tmp_path / "run", custom_dir=task_inputs(tmp_path / "task"))
        inputs.activate(inputs.prepare(), "start")
        expected = inputs.model_instructions()[1]
    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"main-token": "main", "other-token": "other"},
            main_owner="main",
            forward_model=lambda **kwargs: calls.append(deepcopy(kwargs)) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            task_instructions=expected,
            input_versions=inputs,
        )
    )
    context = [{"role": "user", "content": "task"}]
    for token, instructions in (
        ("main-token", expected + "\n\nOwn role"),
        ("other-token", "Own role"),
    ):
        result = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + token},
            json={"input": context, "instructions": "Own role"},
        )
        assert result.status_code == 200
        assert calls[-1]["instructions"] == instructions
        assert calls[-1]["input"] == context
