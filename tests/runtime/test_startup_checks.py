"""A window opens with its two readiness checks already submitted."""

import json

from a3_research_agent.host import startup_checks


def fake_client(job_ids=("a" * 32, "b" * 32)):
    posted = []

    class Client:
        def post(self, path, json=None):
            posted.append((path, json))
            return {"job_id": job_ids[len(posted) - 1]}

    return Client(), posted


def test_both_checks_are_submitted_as_cpu_jobs(tmp_path):
    client, posted = fake_client()
    result = startup_checks.submit(client, tmp_path, "/data1/zsh/a3_dual_arm_sim/ds-merged")

    assert [job["kind"] for job in result["jobs"]] == ["a3_api", "dataset_audit"]
    assert result["errors"] == []
    assert [body["gpu"] for _, body in posted] == [False, False]
    assert posted[0][1]["command"][:2] == ["python", "a3_api_reader.py"]
    assert posted[1][1]["command"][:2] == ["python", "a3_dataset_audit_runner.py"]
    assert posted[1][1]["command"][3] == "/data1/zsh/a3_dual_arm_sim/ds-merged"
    # The script rides inside the request: no session record exists yet.
    assert "workspace_files" not in posted[0][1]
    script = posted[0][1]["files"]["a3_api_reader.py"]
    assert "a3_dual_arm_sim" in script or "def main" in script


def test_a_dataset_check_is_optional(tmp_path):
    client, posted = fake_client()
    result = startup_checks.submit(client, tmp_path, None)
    assert [job["kind"] for job in result["jobs"]] == ["a3_api"]
    assert len(posted) == 1


def test_a_failing_controller_cannot_stop_the_window(tmp_path):
    class Broken:
        def post(self, path, json=None):
            raise RuntimeError("controller unreachable")

    result = startup_checks.submit(Broken(), tmp_path, "/data/ds")
    assert result["jobs"] == []
    assert len(result["errors"]) == 2
    assert "controller unreachable" in result["errors"][0]


def test_the_result_is_recorded_next_to_the_run(tmp_path):
    startup_checks.record(
        tmp_path, {"jobs": [{"kind": "a3_api", "job_id": "c" * 32}], "errors": []}
    )
    written = json.loads((tmp_path / "startup-checks.json").read_text())
    assert written["jobs"][0]["kind"] == "a3_api"


def test_startup_observation_records_final_dataset_report(tmp_path):
    identifier = "a" * 32
    checks = {"jobs": [{"kind": "dataset_audit", "job_id": identifier}], "errors": []}
    startup_checks.record(tmp_path, checks)
    requests = []

    def get(path):
        requests.append(path)
        if "artifacts?" in path:
            return {
                "encoding": "utf-8",
                "content": json.dumps(
                    {
                        "schema": 1,
                        "status": "passed",
                        "metadata": {"episodes": 37, "layouts": {"2x5": 37}},
                        "warnings": ["mixed layout absent"],
                    }
                ),
            }
        return {"job_id": identifier, "status": "completed", "exit_code": 0}

    assert startup_checks.observe(tmp_path, checks, get)
    assert startup_checks.observe(tmp_path, checks, get)
    written = json.loads((tmp_path / "startup-checks.json").read_text())
    job = written["jobs"][0]
    assert job["status"] == "completed"
    assert job["report"] == "dataset-audit-startup/audit.json"
    assert job["result"]["episodes"] == 37
    assert job["result"]["warnings"] == ["mixed layout absent"]
    assert len(requests) == 2
