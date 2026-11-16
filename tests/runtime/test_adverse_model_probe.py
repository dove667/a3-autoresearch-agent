"""End-to-end probe: an uncooperative model keeps making progress.

Every turn here mimics a failure mode seen in weaker models — a prose-only reply,
the same call twice, arguments the contract rejects, then a serial batch — and the
loop must correct each one without losing the run or the job records.
"""

import json

from a3_research_agent.harness import agent, drift


def runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("A3_GATEWAY_TOKEN", "scoped-token")
    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path / "reference"))
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "conversation"))
    monkeypatch.setenv("A3_PLAN_FILE", str(tmp_path / "plan" / "current.json"))
    (tmp_path / "workspace").mkdir()
    (tmp_path / "conversation").mkdir()
    # A fresh run starts from an empty checkpoint the Harness owns.
    (tmp_path / "conversation/checkpoint.json").write_text("[]")

    class Compactor:
        def __init__(self, *args, **kwargs):
            pass

        def before_request(self, messages, **kwargs):
            return messages, None

        def wait(self, **kwargs):
            return None

        def apply_ready(self, messages):
            return messages, None

    monkeypatch.setattr(agent.compaction, "ForkCompactor", Compactor)
    monkeypatch.setattr(agent.context_manager, "configure", lambda request: None)
    monkeypatch.setattr(agent, "req", lambda *args, **kwargs: {"is_main": True})
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)


def scripted(turns, seen):
    sequence = list(turns)

    def request_model(request):
        seen.append(json.loads(json.dumps({k: v for k, v in request.items() if k != "input"})))
        turn = sequence.pop(0) if sequence else {"status": "completed", "output": []}
        return turn

    return request_model


def call(name, call_id, arguments):
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}


def prose():
    return {"status": "completed", "output": [{"role": "assistant", "content": "我先分析一下"}]}


def test_prose_duplicate_and_rejected_calls_are_corrected_then_jobs_are_recorded(
    tmp_path, monkeypatch
):
    runtime(tmp_path, monkeypatch)
    seen = []
    show = json.dumps({"action": "show"})
    turns = [
        prose(),
        {"status": "completed", "output": [call("plan", "p1", show)]},
        {"status": "completed", "output": [call("plan", "p2", show)]},
        {
            "status": "completed",
            "output": [
                call("job_status", "c3", json.dumps({"job_id": "not-a-job"})),
                call("job_status", "c4", json.dumps({"job_id": "still-not-a-job"})),
            ],
        },
        {
            "status": "completed",
            "output": [
                call(
                    "job_submit",
                    "c5",
                    json.dumps({"jobs": [{"command": "true"}, {"command": "true"}]}),
                )
            ],
        },
    ]
    monkeypatch.setattr(agent, "request_model", scripted(turns, seen))
    executed = []

    def dispatch(tool_call):
        executed.append(tool_call["name"])
        if tool_call["call_id"] in {"c3", "c4"}:
            return {
                "ok": False,
                "error": "invalid_arguments",
                "hint": "job_id 必须是 32 位十六进制",
            }
        if tool_call["call_id"] == "c5":
            return {
                "ok": True,
                "jobs": [
                    {"ok": True, "job_id": "b" * 32, "status": "queued"},
                    {"ok": True, "job_id": "c" * 32, "status": "queued"},
                ],
            }
        return {"ok": True, "revision": 1}

    monkeypatch.setattr(agent, "dispatch_tool_call", dispatch)
    agent.main()

    # A prose-only turn is corrected, and the next request forces a tool call.
    assert "tool_choice" not in seen[0]
    assert seen[1]["tool_choice"] == "required"
    history = json.loads(agent.state_paths.path(agent.state_paths.CHECKPOINT).read_text())
    text = json.dumps(history, ensure_ascii=False)
    assert drift.NO_TOOL_NOTICE.strip()[:20] in text
    assert drift.REPEAT_NOTICE.strip()[:20] in text
    assert drift.INVALID_NOTICE.strip()[:20] in text
    assert executed == ["plan", "plan", "job_status", "job_status", "job_submit"]

    ledger_lines = [
        json.loads(line)
        for line in agent.ledger.path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["event"] for event in ledger_lines] == [
        "job_submit_item",
        "job_submit_item",
    ]
    assert [event["job_id"] for event in ledger_lines] == ["b" * 32, "c" * 32]


def test_prose_only_model_stops_after_its_corrections_are_spent(tmp_path, monkeypatch):
    runtime(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        agent,
        "request_model",
        scripted([prose() for _ in range(drift.MAX_NO_TOOL + 4)], seen),
    )
    monkeypatch.setattr(agent, "dispatch_tool_call", lambda tool_call: {"ok": True})
    agent.main()
    # A model that never calls a tool must not spin all night: it ends after the budget.
    assert len(seen) == drift.MAX_NO_TOOL
    assert all(request.get("tool_choice") for request in seen[1:])


def test_forced_tool_choice_is_dropped_when_the_provider_rejects_it(tmp_path, monkeypatch):
    runtime(tmp_path, monkeypatch)
    seen = []

    def request_model(request):
        seen.append(dict(request))
        if "tool_choice" in request and len(seen) < 4:
            raise RuntimeError("HTTP 400: unsupported field tool_choice")
        return prose() if len(seen) < 6 else {"status": "completed", "output": []}

    monkeypatch.setattr(agent, "request_model", request_model)
    monkeypatch.setattr(agent, "dispatch_tool_call", lambda tool_call: {"ok": True})
    agent.main()
    assert "tool_choice" in seen[1]
    assert "tool_choice" not in seen[2]
    assert all("tool_choice" not in request for request in seen[2:])


def test_endless_reading_withholds_tools_until_the_model_states_a_conclusion(tmp_path, monkeypatch):
    runtime(tmp_path, monkeypatch)
    seen = []
    reads = [
        {
            "status": "completed",
            "output": [call("read", f"r{index}", json.dumps({"path": f"f{index}"}))],
        }
        for index in range(drift.ANSWER_TURNS)
    ]
    turns = [
        *reads,
        prose(),
        {"status": "completed", "output": [call("plan", "p2", '{"action":"show"}')]},
    ]
    monkeypatch.setattr(agent, "request_model", scripted(turns, seen))
    monkeypatch.setattr(agent, "dispatch_tool_call", lambda tool_call: {"ok": True, "path": "f"})
    agent.main()

    # Reading turns keep the tools; the turn after the budget arrives without them.
    assert all("tools" in request for request in seen[: drift.ANSWER_TURNS])
    assert "tools" not in seen[drift.ANSWER_TURNS]
    assert "tool_choice" not in seen[drift.ANSWER_TURNS]
    # The demanded text answer is not treated as drift: tool use resumes right after.
    assert "tools" in seen[drift.ANSWER_TURNS + 1]
    assert "tool_choice" not in seen[drift.ANSWER_TURNS + 1]
    history = json.dumps(
        json.loads(agent.state_paths.path(agent.state_paths.CHECKPOINT).read_text()),
        ensure_ascii=False,
    )
    assert drift.ANSWER_NOTICE.strip()[:20] in history
    assert drift.NO_TOOL_NOTICE.strip()[:20] not in history
