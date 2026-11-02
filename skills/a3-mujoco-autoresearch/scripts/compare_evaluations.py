"""Offline paired A3 phase-two 0.3.0 comparison; see references/evaluation-helper.md."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path

CELLS = {(task, level) for task in ("2x5", "2x10")
         for level in ("basic", "medium", "advanced")}
CAPACITY = {"2x5": 10, "2x10": 20}
STEPS = {"2x5": 500, "2x10": 3200}


def integer(value, label):
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer, not a boolean or float")
    return value


def digest(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be a lowercase SHA256")
    return value


def validate(report):
    """Validate explicit normalized input. Does not authenticate referenced files."""
    if not isinstance(report, dict) or report.get("schema") != "a3-paired-evaluation-v1":
        raise ValueError("expected explicit a3-paired-evaluation-v1 input; raw reports need adaptation")
    if report.get("protocol_version") != "0.3.0":
        raise ValueError("this helper implements phase-two protocol 0.3.0 only")
    digest(report.get("protocol_sha256"), "protocol_sha256")
    digest(report.get("conditions_sha256"), "conditions_sha256")
    digest(report.get("policy_snapshot_sha256"), "policy_snapshot_sha256")
    if report.get("split") not in ("development", "final-test"):
        raise ValueError("split must be development or final-test")
    records = report.get("episodes")
    if not isinstance(records, list) or not records:
        raise ValueError("episodes must be a nonempty list")
    mapped = {}
    for i, item in enumerate(records):
        if not isinstance(item, dict):
            raise TypeError(f"episode {i} is not an object")
        task, level = item.get("task"), item.get("difficulty")
        if (task, level) not in CELLS:
            raise ValueError(f"episode {i}: unknown task/difficulty")
        seed = integer(item.get("seed"), "seed")
        if seed < 0 or integer(item.get("inference_seed"), "inference_seed") != seed:
            raise ValueError("inference_seed must equal nonnegative scenario seed")
        key = (task, level, seed)
        if key in mapped:
            raise ValueError(f"duplicate episode {key}")
        if item.get("status") != "completed":
            raise ValueError(f"episode {key} is not completed; do not convert failures to zero")
        count = integer(item.get("cookies_in_target"), "cookies_in_target")
        if not 0 <= count <= CAPACITY[task]:
            raise ValueError(f"episode {key}: count outside task capacity")
        if type(item.get("success")) is not bool:
            raise ValueError(f"episode {key}: success must be a boolean")
        if integer(item.get("max_steps"), "max_steps") != STEPS[task]:
            raise ValueError(f"episode {key}: max_steps differs from protocol")
        hz = item.get("control_hz")
        if isinstance(hz, bool) or not isinstance(hz, (int, float)) or hz != 20:
            raise ValueError(f"episode {key}: control_hz differs from protocol")
        mapped[key] = item
    return mapped


def percentile(sorted_values, p):
    index = (len(sorted_values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (index - lo)


def mean(values):
    return sum(values) / len(values)


def interval(values, repeats, rng):
    if len(values) < 2:
        return None
    draws = sorted(mean(rng.choices(values, k=len(values))) for _ in range(repeats))
    return [percentile(draws, .025), percentile(draws, .975)]


def compare(baseline, candidate, repeats=10000, bootstrap_seed=0):
    if type(repeats) is not int or repeats < 100:
        raise ValueError("bootstrap repeats must be an integer >= 100")
    left, right = validate(baseline), validate(candidate)
    for field in ("protocol_sha256", "conditions_sha256", "split"):
        if baseline[field] != candidate[field]:
            raise ValueError(f"baseline and candidate differ in {field}")
    if left.keys() != right.keys():
        raise ValueError("baseline and candidate episode keys must match exactly")
    grouped = defaultdict(list)
    for key in sorted(left):
        grouped[key[:2]].append(key)
    rng = random.Random(bootstrap_seed)
    output = {
        "schema": "a3-paired-comparison-v1",
        "protocol_version": "0.3.0",
        "split": baseline["split"],
        "paired_episodes": len(left),
        "baseline_snapshot": baseline["policy_snapshot_sha256"],
        "candidate_snapshot": candidate["policy_snapshot_sha256"],
        "bootstrap": {"unit": "paired scenario seed", "repeats": repeats,
                      "random_seed": bootstrap_seed, "confidence": .95},
        "cells": [],
        "provenance_verified": False,
        "adoption_decision": "not made by this helper",
    }
    seed_sets = []
    for cell, keys in sorted(grouped.items()):
        cap = CAPACITY[cell[0]]
        a = [left[k]["cookies_in_target"] for k in keys]
        b = [right[k]["cookies_in_target"] for k in keys]
        differences = [y - x for x, y in zip(a, b)]
        seed_sets.append({k[2] for k in keys})
        output["cells"].append({
            "task": cell[0], "difficulty": cell[1], "n": len(keys),
            "seeds": [k[2] for k in keys],
            "baseline_mean_count": mean(a), "candidate_mean_count": mean(b),
            "baseline_total_count": sum(a), "candidate_total_count": sum(b),
            "baseline_counts": a, "candidate_counts": b,
            "baseline_successes": sum(left[k]["success"] for k in keys),
            "candidate_successes": sum(right[k]["success"] for k in keys),
            "paired_count_difference": mean(differences),
            "paired_count_difference_ci95": interval(differences, repeats, rng),
            "paired_normalized_difference": mean(differences) / cap,
        })
    full = set(grouped) == CELLS and all(len(s) == 20 for s in seed_sets)
    full = full and all(s == seed_sets[0] for s in seed_sets)
    output["complete_grid"] = full
    if full:
        seeds = sorted(seed_sets[0])
        # One draw is a scenario seed, carrying both tasks and all three levels together.
        differences = [mean([
            (right[(t, d, s)]["cookies_in_target"] - left[(t, d, s)]["cookies_in_target"])
            / CAPACITY[t] for t, d in sorted(CELLS)
        ]) for s in seeds]
        ci = interval(differences, repeats, rng)
        output["G"] = {
            "baseline": mean([c["baseline_mean_count"] / CAPACITY[c["task"]]
                              for c in output["cells"]]),
            "candidate": mean([c["candidate_mean_count"] / CAPACITY[c["task"]]
                               for c in output["cells"]]),
            "paired_difference": mean(differences), "paired_difference_ci95": ci,
            "interpretation": ("observed difference; evidence of improvement is insufficient"
                               if ci[0] <= 0 <= ci[1] else
                               "interval excludes zero for these evaluation scenarios; "
                               "does not establish robustness across training seeds"),
        }
    else:
        output["coverage_note"] = (
            "partial or noncanonical grid; cell results only, no formal G. "
            "Full protocol grid needs all six cells and the same 20 scenario seeds per cell."
        )
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--bootstrap-repeats", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=0)
    args = parser.parse_args()
    try:
        raw = [p.read_bytes() for p in (args.baseline, args.candidate)]
        result = compare(*(json.loads(value) for value in raw),
                         repeats=args.bootstrap_repeats, bootstrap_seed=args.bootstrap_seed)
        result["normalized_input_sha256"] = [hashlib.sha256(value).hexdigest() for value in raw]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Invalid comparison: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
