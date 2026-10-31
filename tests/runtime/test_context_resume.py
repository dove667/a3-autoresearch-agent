import json
from copy import deepcopy

import pytest

from runtime.local.context_resume import ContextAgent, load_context


class Model:
    def __init__(self):
        self.seen = []

    def query(self, messages):
        self.seen.append(deepcopy(messages))
        raise ConnectionError("controlled stop")

    def format_message(self, **kwargs):
        return kwargs

    def serialize(self):
        return {}


class Environment:
    def serialize(self):
        return {}


def checkpoint(path, messages):
    path.write_text(
        json.dumps(
            {
                "messages": messages,
                "info": {
                    "model_stats": {"api_calls": 4, "instance_cost": 0.25},
                    "config": {
                        "agent": {"system_template": "original", "instance_template": "original"}
                    },
                },
            }
        )
    )


def test_resume_queries_exact_original_context_without_new_task(tmp_path):
    messages = [
        {"role": "system", "content": "original"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "saved result"},
        {"role": "exit", "extra": {"exit_status": "ConnectionError"}},
    ]
    path = tmp_path / "trajectory.json"
    checkpoint(path, messages)
    model = Model()
    agent = ContextAgent(model, Environment(), system_template="unused", instance_template="unused")
    with pytest.raises(ConnectionError):
        agent.resume(load_context(path))
    assert model.seen == [messages[:-1]]
    assert agent.n_calls == 5
    assert agent.cost == 0.25


def test_resume_refuses_completed_session(tmp_path):
    path = tmp_path / "trajectory.json"
    checkpoint(
        path,
        [
            {"role": "system", "content": "original"},
            {"role": "exit", "extra": {"exit_status": "Submitted"}},
        ],
    )
    with pytest.raises(ValueError, match="submitted"):
        load_context(path)


def test_resume_refuses_unresolved_tool_action(tmp_path):
    path = tmp_path / "trajectory.json"
    checkpoint(
        path,
        [
            {"role": "system", "content": "original"},
            {
                "extra": {"actions": [{"command": "submit-job"}]},
                "output": [{"type": "function_call", "call_id": "call-a"}],
            },
        ],
    )
    with pytest.raises(ValueError, match="tool"):
        load_context(path)


def test_resume_preserves_tool_results_and_provider_context(tmp_path):
    messages = [
        {"role": "system", "content": "original"},
        {
            "output": [
                {"type": "function_call", "call_id": "call-a"},
                {"type": "reasoning", "encrypted_content": "opaque fixture"},
            ],
            "extra": {"actions": [{"command": "submit-job"}]},
        },
        {"type": "function_call_output", "call_id": "call-a", "output": "job-id"},
    ]
    path = tmp_path / "trajectory.json"
    checkpoint(path, messages)
    assert load_context(path)["messages"] == messages
