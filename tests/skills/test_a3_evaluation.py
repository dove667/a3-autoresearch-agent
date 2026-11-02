import copy
import importlib.util
from pathlib import Path

import pytest

PATH = (Path(__file__).parents[2] / "skills/a3-mujoco-autoresearch/scripts/compare_evaluations.py")
spec = importlib.util.spec_from_file_location("a3_compare", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def report(full=False, increment=0):
    cells = sorted(module.CELLS) if full else [("2x5", "basic")]
    seeds = range(20) if full else range(3)
    return {
        "schema": "a3-paired-evaluation-v1", "protocol_version": "0.3.0",
        "protocol_sha256": "a" * 64, "conditions_sha256": "b" * 64,
        "policy_snapshot_sha256": "c" * 64, "split": "development",
        "episodes": [
            {"task": task, "difficulty": level, "seed": seed, "inference_seed": seed,
             "status": "completed", "cookies_in_target": 1 + increment,
             "success": False, "control_hz": 20, "max_steps": module.STEPS[task]}
            for task, level in cells for seed in seeds
        ],
    }


def test_partial_does_not_produce_formal_g():
    result = module.compare(report(), report(increment=1), repeats=100)
    assert not result["complete_grid"]
    assert "G" not in result
    assert result["cells"][0]["paired_count_difference"] == 1
    assert result["provenance_verified"] is False


def test_full_grid_normalization_and_paired_bootstrap():
    result = module.compare(report(True), report(True, 1), repeats=100)
    assert result["complete_grid"]
    assert result["G"]["baseline"] == pytest.approx(.075)
    assert result["G"]["candidate"] == pytest.approx(.15)
    assert result["G"]["paired_difference"] == pytest.approx(.075)
    assert result["G"]["paired_difference_ci95"] == pytest.approx([.075, .075])


def test_cross_cell_correlations_preserved():
    left, right = report(True), report(True)
    for a, b in zip(left["episodes"], right["episodes"]):
        # Variable opposite normalized shifts cancel within each scenario seed.
        a["cookies_in_target"] = b["cookies_in_target"] = 3
        b["cookies_in_target"] += (1 if b["task"] == "2x5" else -2) * (b["seed"] % 2)
    result = module.compare(left, right, repeats=100)
    assert result["G"]["paired_difference_ci95"] == pytest.approx([0, 0])


def test_zero_delta_evidence_insufficient():
    result = module.compare(report(True), report(True), repeats=100)
    assert "insufficient" in result["G"]["interpretation"]


@pytest.mark.parametrize("field,value", [
    ("cookies_in_target", True), ("cookies_in_target", -1), ("cookies_in_target", 11),
    ("status", "crashed"), ("success", 1), ("seed", 1.0), ("inference_seed", 99),
    ("max_steps", 3200), ("control_hz", float("nan")), ("task", "other"),
])
def test_invalid_episode_rejected(field, value):
    bad = report()
    bad["episodes"][0][field] = value
    with pytest.raises(ValueError):
        module.compare(report(), bad, repeats=100)


def test_duplicates_and_missing_pairs_rejected():
    bad = report()
    bad["episodes"].append(copy.deepcopy(bad["episodes"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        module.validate(bad)
    bad = report()
    bad["episodes"].pop()
    with pytest.raises(ValueError, match="keys"):
        module.compare(report(), bad, repeats=100)


@pytest.mark.parametrize("field,value", [
    ("conditions_sha256", "d" * 64), ("protocol_sha256", "e" * 64),
    ("split", "final-test"), ("protocol_version", "0.1.1"),
])
def test_changed_conditions_or_protocol_rejected(field, value):
    bad = report()
    bad[field] = value
    with pytest.raises(ValueError):
        module.compare(report(), bad, repeats=100)


def test_unbalanced_twenty_seeds_not_full_grid():
    left, right = report(True), report(True)
    for obj in (left, right):
        obj["episodes"][0]["seed"] = 123
        obj["episodes"][0]["inference_seed"] = 123
    result = module.compare(left, right, repeats=100)
    assert not result["complete_grid"]
    assert "G" not in result


def test_single_episode_interval_is_unknown():
    left = report()
    left["episodes"] = left["episodes"][:1]
    result = module.compare(left, left, repeats=100)
    assert result["cells"][0]["paired_count_difference_ci95"] is None


def test_raw_report_is_not_silently_accepted():
    with pytest.raises(ValueError, match="explicit"):
        module.validate({"summary": {}, "episodes": []})


def test_cli_readonly_json_output(tmp_path):
    import json
    import subprocess
    import sys

    a, b = tmp_path / "baseline.json", tmp_path / "candidate.json"
    a.write_text(json.dumps(report()))
    b.write_text(json.dumps(report(increment=1)))
    original = (a.read_bytes(), b.read_bytes())
    result = subprocess.run(
        [sys.executable, str(PATH), str(a), str(b), "--bootstrap-repeats", "100"],
        capture_output=True, text=True, check=True,
    )
    output = json.loads(result.stdout)
    assert output["paired_episodes"] == 3
    assert "G" not in output
    assert len(output["normalized_input_sha256"]) == 2
    assert (a.read_bytes(), b.read_bytes()) == original


def test_cli_invalid_report_does_not_emit_score(tmp_path):
    import json
    import subprocess
    import sys

    bad = tmp_path / "bad.json"
    value = report()
    value["episodes"][0]["status"] = "crashed"
    bad.write_text(json.dumps(value))
    result = subprocess.run(
        [sys.executable, str(PATH), str(bad), str(bad)], check=False, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "not completed" in result.stderr
