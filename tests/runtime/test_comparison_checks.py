from runtime.local.comparison_checks import check_comparison


def result(**changes):
    return {
        "status": "completed",
        "report_present": True,
        "protocol": {
            "task": "task-a",
            "seeds": [1, 2, 3],
            "workers": 1,
            "step_budget": 500,
            "randomization": "basic",
            "success_rule": "original",
        },
        **changes,
    }


def test_comparison_reports_missing_fields_failures_and_changed_conditions():
    baseline, candidate = result(), result()
    candidate["protocol"]["workers"] = 4
    del candidate["protocol"]["success_rule"]
    candidate.update(status="failed", report_present=False)
    checked = check_comparison(baseline, candidate)
    assert not checked["comparable"]
    assert checked["differences"]["workers"] == {"baseline": 1, "candidate": 4}
    assert "success_rule" in checked["missing"]["candidate"]
    assert checked["execution_issues"]["candidate"] == ["not_completed", "missing_report"]


def test_equal_protocols_pass_and_additional_conditions_are_compared():
    baseline, candidate = result(), result()
    assert check_comparison(baseline, candidate)["comparable"]
    baseline["protocol"]["a3_version"] = "one"
    assert not check_comparison(baseline, candidate)["comparable"]
    candidate["protocol"]["a3_version"] = "two"
    assert "a3_version" in check_comparison(baseline, candidate)["differences"]


def test_missing_evidence_and_wrong_types_never_pass():
    assert not check_comparison({}, {})["comparable"]
    for invalid in (None, [], "", {}):
        assert not check_comparison(result(), result(protocol=invalid))["comparable"]


def test_seed_and_nested_condition_types_are_distinct():
    baseline, candidate = result(), result()
    candidate["protocol"]["seeds"] = [True, 2, 3]
    checked = check_comparison(baseline, candidate)
    assert not checked["comparable"]
    assert "invalid_seeds" in checked["execution_issues"]["candidate"]
    baseline, candidate = result(), result()
    baseline["protocol"]["extra"] = {"nested": [1]}
    candidate["protocol"]["extra"] = {"nested": [True]}
    assert "extra" in check_comparison(baseline, candidate)["differences"]
