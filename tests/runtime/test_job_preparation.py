"""One saved GPU request carries source integrity and submission identity."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from a3_research_agent.harness import job_tools
from a3_research_agent.host.sessions.workspace import read_workspace_files

CPU_ID = "a" * 32
GPU_ID = "b" * 32


def request_fixture():
    calls = []
    status = {"job_id": CPU_ID, "status": "completed", "exit_code": 0}
    report = {"schema": 1, "status": "passed"}

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"job_id": GPU_ID if body["gpu"] else CPU_ID, "status": "queued"}
        if "artifacts?" in path:
            return {
                "encoding": "base64",
                "data": base64.b64encode(json.dumps(report).encode()).decode(),
            }
        return status

    return calls, status, report, request


def gpu_request(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/train.py").write_text("print('research')")
    (tmp_path / "experiments/trial").mkdir(parents=True)
    (tmp_path / "experiments/trial/config.json").write_text('{"steps": 5000}')
    return {
        "command": ["python", "train.py", "--config", "config.json"],
        "attachments": [
            {"path": "scripts/train.py", "destination": "train.py"},
            {"path": "experiments/trial/config.json", "destination": "config.json"},
        ],
        "gpu_memory_mb": 6000,
        "input_exports": ["export-" + "c" * 32],
    }


def test_saved_request_attaches_config_and_submits_once_after_real_cpu_pass(tmp_path):
    args = gpu_request(tmp_path)
    calls, status, report, request = request_fixture()
    checked = job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)
    assert checked["preflight_job_id"] == CPU_ID
    cpu = calls[0][2]
    assert cpu["gpu"] is False and "gpu_memory_mb" not in cpu
    assert cpu["command"] == ["env", "A3_RESEARCH_PREFLIGHT=1", *args["command"]]
    assert [item["path"] for item in cpu["workspace_files"]] == [
        "scripts/train.py",
        "experiments/trial/config.json",
    ]
    files, _ = read_workspace_files(tmp_path, cpu["workspace_files"])
    assert json.loads(files["experiments/trial/config.json"])["steps"] == 5000
    # Repeated preparation neither sends a second CPU check nor needs source hashes from the model.
    assert job_tools.dispatch("job_prepare", args, request, workspace=tmp_path) == checked
    submit = {"prepared_id": checked["prepared_id"]}
    status.update(status="failed", exit_code=1)
    assert (
        job_tools.dispatch("job_submit", submit, request, workspace=tmp_path)["error"]
        == "preflight_not_passed"
    )
    status.update(status="completed", exit_code=0)
    report.clear()
    assert (
        job_tools.dispatch("job_submit", submit, request, workspace=tmp_path)["error"]
        == "preflight_report_missing"
    )
    report.update(schema=1, status="passed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: job_tools.dispatch("job_submit", submit, request, workspace=tmp_path),
                range(2),
            )
        )
    assert all(result["job_id"] == GPU_ID for result in results)
    production = [body for method, _, body in calls if method == "POST" and body["gpu"]]
    assert len(production) == 1
    assert production[0]["workspace_files"] == cpu["workspace_files"]
    assert production[0]["input_exports"] == cpu["input_exports"]
    assert production[0]["command"] == args["command"]


def test_changed_attachment_requires_preparing_new_request(tmp_path):
    args = gpu_request(tmp_path)
    _, _, _, request = request_fixture()
    checked = job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)
    (tmp_path / "experiments/trial/config.json").write_text('{"steps": 6000}')
    assert (
        job_tools.dispatch(
            "job_submit", {"prepared_id": checked["prepared_id"]}, request, workspace=tmp_path
        )["error"]
        == "prepared_inputs_changed"
    )
    new = job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)
    assert new["prepared_id"] != checked["prepared_id"]


def test_unknown_gpu_send_is_fenced_and_never_repeated(tmp_path):
    args = gpu_request(tmp_path)
    _calls, _, _, base_request = request_fixture()
    checked = job_tools.dispatch("job_prepare", args, base_request, workspace=tmp_path)
    sends = []

    def request(method, path, body=None):
        if method == "POST" and body["gpu"]:
            sends.append(body)
            raise ConnectionResetError("lost after send")
        return base_request(method, path, body)

    submit = {"prepared_id": checked["prepared_id"]}
    first = job_tools.dispatch("job_submit", submit, request, workspace=tmp_path)
    assert first["error"] == "production_submission_unknown"
    assert job_tools.dispatch("job_submit", submit, request, workspace=tmp_path) == first
    assert len(sends) == 1


def test_submission_rejects_old_fields_and_prepares_a_direct_gpu_submit(tmp_path):
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path))
        if path == "/jobs":
            return {"job_id": CPU_ID}
        return {"status": "queued", "exit_code": None}

    for args in (
        {"command": ["python", "-c", "print(1)"], "files": {"run.py": "print(1)"}},
        {"command": ["python", "-c", "print(1)"], "workspace_files": []},
        {"prepared_id": "prepared-" + "a" * 32, "command": ["python"]},
    ):
        result = job_tools.dispatch("job_submit", args, request, workspace=tmp_path)
        assert result.get("error"), result
    # A GPU request is prepared in code rather than bounced back to the model.
    gpu = job_tools.dispatch(
        "job_submit",
        {"command": ["python", "-c", "print(1)"], "gpu": True},
        request,
        workspace=tmp_path,
    )
    assert gpu["status"] == "prepared_pending_preflight"
    assert gpu["prepared_id"].startswith("prepared-")
    assert all(method == "POST" or "/preflight" in path for method, path in calls)


def test_cpu_attachment_hashes_are_computed_for_root_or_experiment_files(tmp_path):
    source = tmp_path / "config.json"
    source.write_text('{"batch": 8}')
    calls = []
    result = job_tools.dispatch(
        "job_submit",
        {"command": ["python", "-m", "run"], "attachments": [{"path": str(source)}]},
        lambda *args: calls.append(args) or {"job_id": CPU_ID},
        workspace=tmp_path,
    )
    assert result["job_id"] == CPU_ID
    assert calls[0][2]["gpu"] is False
    assert calls[0][2]["workspace_files"] == [
        {"path": "config.json", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    ]


def test_attachment_boundaries_and_destination_conflicts_are_rejected(tmp_path):
    (tmp_path / "ok.txt").write_text("ok")
    (tmp_path / "binary").write_bytes(b"\xff")
    (tmp_path / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    (tmp_path / "link").symlink_to(tmp_path / "ok.txt")
    bad = [
        [{"path": "../outside"}],
        [{"path": "link"}],
        [{"path": "binary"}],
        [{"path": "large"}],
        [{"path": "ok.txt", "sha256": "0" * 64}],
        [{"path": "ok.txt", "destination": "x"}, {"path": "ok.txt", "destination": "x/config"}],
    ]
    calls = []
    for attachments in bad:
        result = job_tools.dispatch(
            "job_submit",
            {"command": ["python"], "attachments": attachments},
            lambda *args: calls.append(args),
            workspace=tmp_path,
        )
        assert result.get("error"), result
    assert not calls


def test_preparation_retries_only_known_failed_cpu_checks(tmp_path):
    args = gpu_request(tmp_path)
    calls, status, _, request = request_fixture()
    first = job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)
    status.update(status="failed", exit_code=1)
    second = job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)
    assert second["prepared_id"] == first["prepared_id"]
    assert len([call for call in calls if call[0] == "POST"]) == 2


def test_rejected_preflight_does_not_block_corrected_deployment_retry(tmp_path):
    args = gpu_request(tmp_path)
    first = job_tools.dispatch(
        "job_prepare", args, lambda *args: {"http_error": 403, "body": "denied"}, workspace=tmp_path
    )
    assert first["http_error"] == 403
    _, _, _, request = request_fixture()
    assert (
        job_tools.dispatch("job_prepare", args, request, workspace=tmp_path)["preflight_job_id"]
        == CPU_ID
    )
