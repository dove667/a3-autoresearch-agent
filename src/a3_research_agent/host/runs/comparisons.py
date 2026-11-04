"""Check declared comparison conditions without selecting the better candidate."""

import json

REQUIRED_CONDITIONS = {"task", "seeds", "workers", "step_budget", "randomization", "success_rule"}


def same_json_value(left, right) -> bool:
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (ValueError, TypeError):
        return False


def check_comparison(baseline: dict, candidate: dict) -> dict:
    protocols, missing, execution_issues = {}, {}, {}
    for label, evidence in (("baseline", baseline), ("candidate", candidate)):
        protocol = evidence.get("protocol")
        protocol = protocol if isinstance(protocol, dict) else {}
        protocols[label] = protocol
        missing[label] = sorted(
            key
            for key in REQUIRED_CONDITIONS
            if key not in protocol or protocol[key] is None or protocol[key] == ""
        )
        issues = []
        if evidence.get("status") != "completed":
            issues.append("not_completed")
        if evidence.get("report_present") is not True:
            issues.append("missing_report")
        seeds = protocol.get("seeds")
        if not isinstance(seeds, list) or not seeds or any(type(seed) is not int for seed in seeds):
            issues.append("invalid_seeds")
        for key in ("workers", "step_budget"):
            if type(protocol.get(key)) is not int or protocol[key] < 1:
                issues.append("invalid_" + key)
        execution_issues[label] = issues
    left, right = protocols["baseline"], protocols["candidate"]
    differences = {
        key: {"baseline": left.get(key), "candidate": right.get(key)}
        for key in sorted(left.keys() | right.keys())
        if key not in left or key not in right or not same_json_value(left[key], right[key])
    }
    return {
        "comparable": not differences
        and not any(missing.values())
        and not any(execution_issues.values()),
        "missing": missing,
        "differences": differences,
        "execution_issues": execution_issues,
        "scope": "Declared conditions only; verify against raw reports before drawing conclusions",
    }
