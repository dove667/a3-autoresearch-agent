"""Controller entry points stay usable without the Host dependency environment."""

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
        assert "--job-python" in result.stdout
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
