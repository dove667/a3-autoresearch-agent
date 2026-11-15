"""Stopping a research process preserves progress without declaring the goal solved."""

import json

import pytest

from a3_research_agent.host.runs.supervisor import RunSupervisor, evaluate_outcome


def test_research_is_observed_until_exit_without_feedback_or_relaunch(tmp_path):
    child = {"session_id": "main", "status": "running"}
    waits, retained = [], []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 160:
            (tmp_path / "run-outcome.json").write_text('{"status":"complete"}')
            child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        sessions=lambda: [dict(child)],
        retain=lambda: retained.append(True),
        wait=wait,
    ).run()
    assert result["status"] == "incomplete"
    assert len(waits) == 160 and set(waits) == {15}
    assert retained == [True]


def test_supervisor_records_changes_instead_of_identical_heartbeats(tmp_path):
    supervisor = RunSupervisor(run=tmp_path, workspace=tmp_path, sessions=list)
    sessions = [{"session_id": "main", "status": "running"}]
    supervisor.record("supervising", {}, sessions)
    supervisor.record("supervising", {}, sessions)
    sessions[0]["status"] = "exited"
    supervisor.record("supervising", {}, sessions)
    events = [
        json.loads(line)
        for line in (tmp_path / "logs/supervisor-events.jsonl").read_text().splitlines()
    ]
    assert [event["sessions"][0]["status"] for event in events] == ["running", "exited"]


@pytest.mark.parametrize("declaration", [None, {"status": "continue"}, {"status": "complete"}])
def test_no_declaration_can_finish_the_long_term_goal(tmp_path, declaration):
    if declaration is not None:
        (tmp_path / "run-outcome.json").write_text(json.dumps(declaration))
    result = RunSupervisor(run=tmp_path, workspace=tmp_path, sessions=list).run()
    assert result["status"] == "incomplete"


def test_genuine_external_blocker_requires_action_and_evidence(tmp_path):
    (tmp_path / "auth-failure.txt").write_text("Controlled authentication failure")
    outcome = {
        "status": "blocked",
        "external_blocker": {
            "kind": "authentication",
            "requires_human": True,
            "reason": "Login expired",
            "required_action": "Renew authentication",
            "evidence": ["auth-failure.txt"],
        },
    }
    (tmp_path / "run-outcome.json").write_text(json.dumps(outcome))
    assert evaluate_outcome(tmp_path)["status"] == "blocked"
    outcome["external_blocker"]["required_action"] = ""
    (tmp_path / "run-outcome.json").write_text(json.dumps(outcome))
    assert evaluate_outcome(tmp_path)["status"] == "continue"


def test_snapshot_failure_preserves_a_resumable_outcome(tmp_path):
    def retain():
        raise OSError("controlled save failure")

    result = RunSupervisor(run=tmp_path, workspace=tmp_path, sessions=list, retain=retain).run()
    assert result["status"] == "incomplete"
    assert result["retention_error"] == "OSError"


def test_final_record_uses_current_inputs_after_closing_updates(tmp_path):
    phases = []

    def current_inputs():
        assert phases == ["closing"]
        return {"revision": "revision-0002"}

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        sessions=list,
        input_manifest=current_inputs,
        on_finishing=lambda: phases.append("closing"),
    ).run()
    assert result["status"] == "incomplete"
    assert result["input_manifest"]["revision"] == "revision-0002"


def test_running_milestone_never_stops_the_process(tmp_path):
    (tmp_path / "run-outcome.json").write_text(
        '{"status":"continue","reason":"Milestone saved; next experiment pending"}'
    )
    child = {"session_id": "main", "status": "running"}
    waits, retained = [], []

    def wait(seconds):
        waits.append(seconds)
        assert not (tmp_path / "outcome.json").exists()
        assert not retained
        if len(waits) == 3:
            child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        sessions=lambda: [dict(child)],
        retain=lambda: retained.append(True),
        wait=wait,
    ).run()
    assert len(waits) == 3 and retained == [True]
    assert result["status"] == "incomplete"
