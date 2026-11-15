import json

from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.runs.supervisor import RunSupervisor, evaluate_outcome


def complete_result(workspace):
    (workspace / "evidence.txt").write_text("controlled fixture evidence")
    (workspace / "checkpoint.json").write_text("[]")
    (workspace / "continuation.json").write_text(
        json.dumps({"version": 1, "command": COMMAND, "files": ["checkpoint.json"]})
    )
    (workspace / "run-outcome.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "acceptance": {
                    key: {"status": "passed", "evidence": ["evidence.txt"]} for key in "ABCD"
                },
            }
        )
    )


def test_research_session_is_observed_until_exit_without_feedback(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = {"session_id": "child", "status": "running"}
    calls, waits = [], []

    def execute(reason, trajectory):
        calls.append((reason, trajectory))

    def wait(seconds):
        waits.append(seconds)
        assert not (workspace / "host-supervision.json").exists()
        # More than the removed five-minute interval must not recreate the builder.
        if len(waits) == 160:
            complete_result(workspace)
            child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=workspace,
        required_checks=tuple("ABCD"),
        sessions=lambda: [dict(child)],
        wait=wait,
    ).run()
    assert result["status"] == "complete"
    assert not calls
    assert len(waits) == 160
    assert set(waits) == {15}


def test_supervisor_records_changes_instead_of_identical_heartbeats(tmp_path):
    supervisor = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=(),
        sessions=list,
    )
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


def test_incomplete_submission_stops_without_assigning_another_task(tmp_path):
    calls = []
    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=list,
    ).run()
    assert result["status"] == "incomplete"
    assert not calls


def test_completed_claim_requires_all_checks_evidence_and_runnable_selection(tmp_path):
    complete_result(tmp_path)
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "complete"
    (tmp_path / "evidence.txt").unlink()
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"
    (tmp_path / "evidence.txt").write_text("evidence")
    (tmp_path / "continuation.json").unlink()
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"


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
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "blocked"
    outcome["external_blocker"]["required_action"] = ""
    (tmp_path / "run-outcome.json").write_text(json.dumps(outcome))
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"


def test_snapshot_failure_does_not_restart_agent(tmp_path):
    complete_result(tmp_path)
    calls = []

    def finalize():
        raise OSError("controlled save failure")

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=list,
        finalize=finalize,
    ).run()
    assert result["status"] == "archive_error"
    assert not calls


def test_temporary_capacity_is_not_an_external_blocker(tmp_path):
    (tmp_path / "blocker.txt").write_text("candidate_capacity")
    (tmp_path / "run-outcome.json").write_text(
        json.dumps(
            {
                "status": "blocked",
                "external_blocker": {
                    "kind": "candidate_capacity",
                    "reason": "Two candidates active",
                    "required_action": "Wait for a slot",
                    "evidence": ["blocker.txt"],
                },
            }
        )
    )
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"


def test_final_check_uses_active_input_manifest_and_closes_updates_first(tmp_path):
    complete_result(tmp_path)
    phases = []

    def current_inputs():
        assert phases == ["closing"]
        return {"revision": "revision-0002", "acceptance_checks": list("ABCDE")}

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=list,
        input_manifest=current_inputs,
        on_finishing=lambda: phases.append("closing"),
    ).run()
    assert result["status"] == "incomplete"
    assert result["input_manifest"]["revision"] == "revision-0002"


def test_retained_session_never_launches_generic_builder(tmp_path):
    child = {"session_id": "retained", "status": "running"}
    calls = []

    def wait(seconds):
        child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=lambda: [dict(child)],
        wait=wait,
    ).run()
    assert result["status"] == "incomplete"
    assert not calls


def test_result_comes_from_final_main_after_all_workers_stop(tmp_path):
    final = tmp_path / "final-main"
    final.mkdir()
    complete_result(final)
    active = [{"session_id": "new-main", "status": "running"}]

    def wait(seconds):
        active.clear()

    def select():
        assert not active
        return final

    outcome = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=lambda: list(active),
        wait=wait,
        result_workspace=select,
    ).run()
    assert outcome["status"] == "complete"


def test_passed_milestones_with_continue_do_not_end_active_research(tmp_path):
    complete_result(tmp_path)
    path = tmp_path / "run-outcome.json"
    outcome = json.loads(path.read_text())
    outcome.update(status="continue", reason="Milestone saved; next experiment pending")
    path.write_text(json.dumps(outcome))
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"
    child = {"session_id": "main", "status": "running"}
    waits, finalized = [], []

    def wait(seconds):
        waits.append(seconds)
        state = json.loads((tmp_path / "supervision.json").read_text())
        assert state["phase"] == "supervising"
        assert not (tmp_path / "outcome.json").exists()
        assert not finalized
        if len(waits) == 3:
            child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=lambda: [dict(child)],
        finalize=lambda: finalized.append(True),
        wait=wait,
    ).run()
    assert len(waits) == 3
    assert result["status"] == "incomplete"
    assert not finalized
