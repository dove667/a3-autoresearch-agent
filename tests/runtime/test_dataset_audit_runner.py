"""The maintained readiness runner supports deployed A3 auditor signatures."""

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from a3_research_agent.harness import dataset_audit_runner


def test_a3_dataset_auditor_accepts_path_and_required_repo_id(tmp_path, monkeypatch):
    module = ModuleType("a3_dual_arm_sim.data.audit")

    def audit(root, *, repo_id):
        assert isinstance(root, Path) and root == tmp_path
        return {"repo_id": repo_id}

    module.audit_training_dataset = audit
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert dataset_audit_runner.audit(tmp_path, "local/training") == {"repo_id": "local/training"}


def test_a3_dataset_auditor_does_not_hide_an_internal_type_error(tmp_path, monkeypatch):
    module = ModuleType("a3_dual_arm_sim.data.audit")

    def audit(root, *, repo_id):
        raise TypeError("real audit failure")

    module.audit_training_dataset = audit
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(TypeError, match="real audit failure"):
        dataset_audit_runner.audit(tmp_path, "local/training")


def test_missing_a3_auditor_reports_metadata_only(tmp_path, monkeypatch):
    dataset = tmp_path / "training"
    outputs = tmp_path / "outputs"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta/info.json").write_text('{"total_episodes":1}')
    (dataset / "a3_episode_metadata.jsonl").write_text('{"task":"2x5","success":true}\n')
    monkeypatch.setenv("A3_OUTPUTS", str(outputs))
    monkeypatch.setitem(sys.modules, "a3_dual_arm_sim", None)
    assert dataset_audit_runner.main(["--dataset-root", str(dataset), "--output-dir", "audit"]) == 0
    report = json.loads((outputs / "audit/audit.json").read_text())
    assert report["metadata"]["episodes"] == 1
    assert report["validation"] == "metadata_only"
