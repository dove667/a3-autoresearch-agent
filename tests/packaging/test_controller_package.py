"""Controller entry points stay usable without the Host dependency environment."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "src/a3_research_agent/execution/controller/__main__.py"


def test_controller_standalone_entries_need_only_standard_library(tmp_path):
    package_entry = ROOT / "src/a3_research_agent/execution/controller/__main__.py"
    assert package_entry.is_file(), "Controller subprocess entry is absent"
    for entry in (package_entry,):
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(entry), "--help"],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "--share-gpus" in result.stdout
        assert "--run-storage-bytes" in result.stdout


def test_child_command_targets_self_contained_controller_entry(tmp_path):
    from a3_research_agent.execution.controller.entry import controller_command

    command = controller_command("--watch", "--help")
    result = subprocess.run(
        [command[0], "-I", "-S", *command[1:]],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--lock-fd" in result.stdout
    result = subprocess.run(
        [command[0], "-I", "-S", command[1], "--seal-export", "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--storage-limit" in result.stdout


def test_controller_denies_unknown_owner_without_host_imports(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(ENTRY),
            "--root",
            str(tmp_path / "jobs"),
            "--image",
            "unused",
            "status",
        ],
        cwd=tmp_path,
        input=json.dumps({"experiment_id": "other", "job_id": "0" * 32}),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["error"] == "PermissionError"


def test_standalone_watcher_archives_fake_container_and_preserves_job(tmp_path, monkeypatch):
    """Run the real watcher process against a disposable fake Docker executable."""
    import hashlib
    import os

    from a3_research_agent.execution.controller.entry import controller_command
    from a3_research_agent.execution.controller.store import JobStore

    store = JobStore(tmp_path / "jobs")
    job = store.create("owner", ["true"], False, {"keep.txt": "evidence"}, run_id="run-one")
    job["container_id"] = "fake-container"
    store.save(job)
    state_file = tmp_path / "fake-state.json"
    info = {
        "Id": job["container_id"],
        "Name": f"/a3-job-{job['job_id']}",
        "State": {"Status": "created", "ExitCode": None},
        "Config": {
            "Labels": {
                "a3.root": hashlib.sha256(str(store.root).encode()).hexdigest(),
                "a3.experiment": "owner",
                "a3.job": job["job_id"],
            }
        },
    }
    state_file.write_text(json.dumps(info))
    commands = tmp_path / "commands"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        f"#!{sys.executable} -S\n"
        "import json, os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])\n"
        "info = json.loads(p.read_text())\n"
        "op = sys.argv[1]\n"
        "if op == 'start': info['State']['Status'] = 'running'\n"
        "elif op == 'wait':\n"
        "    info['State'] = {'Status': 'exited', 'ExitCode': 0}\n"
        "    print(0)\n"
        "elif op == 'inspect':\n"
        "    if info['State']['Status'] == 'removed':\n"
        "        print('No such container', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    print(json.dumps(info['State'] if '--format' in sys.argv else [info]))\n"
        "elif op == 'logs': print('kept fake output')\n"
        "elif op == 'rm': info['State']['Status'] = 'removed'\n"
        "else: raise SystemExit('Unexpected fake Docker operation: ' + op)\n"
        "p.write_text(json.dumps(info))\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state_file))
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.defpath)
    command = controller_command(
        "--watch", "--root", str(store.root), "--job-id", job["job_id"], "--timeout", "0"
    )
    result = subprocess.run(
        [command[0], "-I", "-S", *command[1:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    saved = store.load(job["job_id"], "owner")
    assert saved["status"] == "completed"
    assert saved["watcher_ended"] is True
    assert json.loads(state_file.read_text())["State"]["Status"] == "removed"
    archive = store.metadata_path(job["job_id"]).parent / "terminal-archive.json"
    assert json.loads(archive.read_text())["job"]["job_id"] == job["job_id"]
    assert (store.workspace(job) / "keep.txt").read_text() == "evidence"


def test_standalone_sealer_seals_only_selected_snapshot(tmp_path):
    from a3_research_agent.execution.controller.entry import controller_command
    from a3_research_agent.execution.exports.readonly import ReadonlyExports

    root = tmp_path / "jobs"
    root.mkdir()
    exports = ReadonlyExports(root)
    source = tmp_path / "weights"
    source.mkdir()
    (source / "weights.bin").write_bytes(b"binary checkpoint")
    record = exports.create("owner", "run-one", source, "job-one", "outputs", "weights")
    command = controller_command(
        "--seal-export",
        "--root",
        str(root),
        "--export-id",
        record["export_id"],
        "--storage-limit",
        "0",
    )
    result = subprocess.run(
        [command[0], "-I", "-S", *command[1:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    saved = exports.read("owner", record["export_id"])
    assert saved["status"] == "ready"
    assert (exports.data_path("owner", record["export_id"]) / "weights.bin").read_bytes() == (
        b"binary checkpoint"
    )
    assert (source / "weights.bin").read_bytes() == b"binary checkpoint"
