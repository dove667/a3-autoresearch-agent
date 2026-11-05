"""Harness imports and worker payloads must be independent of the source checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_agent_import_does_not_read_credentials_or_start_loop(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(source),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import a3_research_agent.harness.agent"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_exported_sources_can_create_complete_child_payload_without_checkout(tmp_path):
    from a3_research_agent.packaging.harness_payload import build_harness_payload

    files = build_harness_payload(
        {"web_tools.py": "TOOLS = []\nROUTES = {}\n", "skill_tools.py": "TOOLS = []\nROUTES = {}\n"}
    )
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    script = """
import json
from a3_research_agent.harness.orchestration import assignments as work_assignments
arguments = dict(id='isolated', role='harness', objective='check sources', stop_condition='done', deliverables=['report.json'], context_files=[], read_only_exports=[])
task, files = work_assignments.worker_package(arguments, coordinator_session_id='main')
print(json.dumps({'files': sorted(files), 'source_files': sorted(task['source_hashes'])}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    exported = json.loads(result.stdout)
    assert set(files) <= set(exported["source_files"])
    assert set(files) <= set(exported["files"])
    assert {"worker-task.json", "checkpoint.json"} <= set(exported["files"])
