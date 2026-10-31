import json

import pytest

from runtime.local.run_supervisor import RunSupervisor, evaluate_outcome


def complete_result(workspace):
    (workspace / "evidence.txt").write_text("controlled fixture evidence")
    (workspace / "entry.py").write_text("print('controlled fixture')\n")
    (workspace / "continuation.json").write_text(
        json.dumps({"command": ["python", "entry.py"], "files": ["entry.py"]})
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


def test_builder_is_started_once_and_waits_without_feedback(tmp_path):
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
        execute_builder=execute,
        wait=wait,
    ).run()
    assert result["status"] == "complete"
    assert len(calls) == 1
    assert calls[0][0] == ""
    assert len(waits) == 160


def test_incomplete_submission_stops_without_assigning_another_task(tmp_path):
    calls = []
    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=list,
        execute_builder=lambda *args: calls.append(args),
    ).run()
    assert result["status"] == "incomplete"
    assert len(calls) == 1


def test_completed_claim_requires_all_checks_evidence_and_runnable_selection(tmp_path):
    complete_result(tmp_path)
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "complete"
    (tmp_path / "evidence.txt").unlink()
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"
    (tmp_path / "evidence.txt").write_text("evidence")
    (tmp_path / "continuation.json").unlink()
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"


@pytest.mark.parametrize("name", ["./entry.py", ".env"])
def test_completed_claim_requires_a_versionable_source_manifest(tmp_path, name):
    complete_result(tmp_path)
    if name == ".env":
        (tmp_path / name).write_text("controlled fixture only")
    (tmp_path / "continuation.json").write_text(
        json.dumps({"command": ["python", "entry.py"], "files": [name]})
    )
    assert evaluate_outcome(tmp_path, tuple("ABCD"))["status"] == "continue"


@pytest.mark.parametrize("count,size", [(129, 1), (1, 20 * 1024 * 1024 + 1)])
def test_completed_claim_respects_snapshot_file_and_byte_limits(tmp_path, count, size):
    complete_result(tmp_path)
    names = []
    for index in range(count):
        name = f"source_{index}.py"
        (tmp_path / name).write_text("x" * size)
        names.append(name)
    (tmp_path / "continuation.json").write_text(
        json.dumps({"command": ["python", names[0]], "files": names})
    )
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
        execute_builder=lambda *args: calls.append(args),
        finalize=finalize,
    ).run()
    assert result["status"] == "archive_error"
    assert len(calls) == 1


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
        execute_builder=lambda *args: calls.append(args),
        wait=wait,
    ).run(initial_builder=False)
    assert result["status"] == "incomplete"
    assert not calls


def test_builder_error_preserves_workers_without_retry(tmp_path):
    child = {"session_id": "child", "status": "running"}
    calls = []

    def execute(*args):
        calls.append(args)
        raise ConnectionError("controlled error")

    def wait(seconds):
        child["status"] = "exited"

    result = RunSupervisor(
        run=tmp_path,
        workspace=tmp_path,
        required_checks=tuple("ABCD"),
        sessions=lambda: [dict(child)],
        execute_builder=execute,
        wait=wait,
    ).run()
    assert result["status"] == "incomplete"
    assert result["builder_error"] == "ConnectionError"
    assert len(calls) == 1
    assert child["status"] == "exited"
