"""Harness imports and source payloads must be independent of the source checkout."""

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


def test_exported_sources_can_reproduce_payload_without_checkout(tmp_path):
    from a3_research_agent.harness_payload import build_harness_payload

    files = build_harness_payload()
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    script = """
import json
from a3_research_agent.harness.agent import tools
from a3_research_agent.harness_payload import build_harness_payload
print(json.dumps({"files": build_harness_payload(), "tools": [x["name"] for x in tools]}))
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
    assert exported["files"] == files
    assert {
        "read",
        "job_wait",
        "idle_wait",
        "skills",
        "plan",
        "memory",
        "job_prepare",
        "research_status",
        "vision_inspect",
        "export_manifest",
        "job_submit",
        "job_artifact",
        "search_files",
        "edit",
        "web_search",
        "web_fetch",
        "web_status",
    } <= set(exported["tools"])

    assert {"gateway", "wait", "progress", "preflight", "job_export"}.isdisjoint(exported["tools"])
