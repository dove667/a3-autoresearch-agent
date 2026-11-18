"""Describe a training dataset before a run spends GPU time on it.

The A3 auditor answers whether a dataset loads and how big it is. It does not say
which tasks those episodes belong to, whether they are labelled successful, or
what the stored action mode is, and those decide whether a mixed-training
experiment can answer its own question. This launcher adds that breakdown and
names every gap it finds.

It answers the CPU preflight (`A3_RESEARCH_PREFLIGHT=1`) and otherwise writes
`audit.json` under `A3_OUTPUTS`.

Run it through the `dataset_audit` tool; it is not a research script.
"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

REPORT = "a3-preflight.json"
OUTPUT = "audit.json"
TASK_HINTS = (("2x10", "2x10"), ("2x5", "2x5"))
EXPECTED_STATE = 16


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dataset-root", required=True)
    result.add_argument("--repo-id")
    result.add_argument("--output-dir", required=True)
    return result


def load_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def episodes(path):
    """Read per-episode metadata rows, tolerating a dataset without them."""
    rows, file = [], Path(path)
    if not file.is_file():
        return rows
    try:
        with file.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def layout(task):
    text = task.casefold() if isinstance(task, str) else ""
    for needle, name in TASK_HINTS:
        if needle in text:
            return name
    return "unknown"


def audit(dataset_root, repo_id):
    """Call the deployed auditor through whichever layout the package exposes."""
    from a3_dual_arm_sim.learning.training import audit_training_dataset

    try:
        from a3_dual_arm_sim.learning.act_training import audit_training_dataset as act_audit
    except ImportError:
        act_audit = None
    for candidate in (act_audit, audit_training_dataset):
        if candidate is None:
            continue
        try:
            value = candidate(dataset_root, repo_id=repo_id) if repo_id else candidate(dataset_root)
            return value if isinstance(value, dict) else {"result": repr(value)}
        except TypeError:
            continue
    raise SystemExit("the deployed A3 package exposes no usable dataset auditor")


def breakdown(rows):
    tasks = Counter(row.get("task") for row in rows if isinstance(row.get("task"), str))
    return {
        "episodes": len(rows),
        "layouts": dict(Counter(layout(row.get("task")) for row in rows)),
        "tasks": {name: count for name, count in tasks.most_common()},
        "successful": sum(1 for row in rows if row.get("success") is True),
        "failed": sum(1 for row in rows if row.get("success") is False),
        "unrated": sum(1 for row in rows if row.get("success") is None),
        "seeds": len({row.get("seed") for row in rows if row.get("seed") is not None}),
        "frames": sum(row.get("frames") or 0 for row in rows),
        "stored_action_mode": dict(
            Counter(str(row.get("stored_action_mode")) for row in rows)
        ),
        "source_action_mode": dict(
            Counter(str(row.get("source_action_mode")) for row in rows)
        ),
    }


def warnings(summary, counts, info):
    result = []
    if summary and summary.get("complete_demonstrations") is False:
        result.append("complete_demonstrations is false: episodes may end before the task does")
    layouts = counts.get("layouts") or {}
    if len(layouts) < 2:
        present = ", ".join(sorted(layouts)) or "none"
        result.append(
            f"only one task layout present ({present}); a mixed run cannot measure the other one"
        )
    if counts and counts.get("failed"):
        result.append(f"{counts['failed']} episodes are marked failed")
    if counts and not counts.get("seeds"):
        result.append("no seeds recorded; duplicate segments cannot be detected from metadata")
    expected = info.get("total_episodes")
    if isinstance(expected, int) and counts and expected != counts["episodes"]:
        result.append(
            f"meta/info.json reports {expected} episodes but metadata has {counts['episodes']}"
        )
    task_list = (info.get("total_tasks"), len(counts.get("tasks") or {}))
    if all(isinstance(value, int) for value in task_list) and task_list[0] != task_list[1]:
        result.append(
            f"meta/info.json reports {task_list[0]} tasks but metadata has {task_list[1]}"
        )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(args.dataset_root)
    if not root.is_dir():
        raise SystemExit(f"dataset directory missing: {root}")
    info = load_json(root / "meta" / "info.json")
    summary = load_json(root / "collection_summary.json")
    rows = episodes(root / "a3_episode_metadata.jsonl")
    counts = breakdown(rows)
    report = {
        "schema": 1,
        "status": "passed",
        "dataset_root": str(root),
        "repo_id": args.repo_id,
        "info": {
            key: info.get(key)
            for key in ("total_episodes", "total_frames", "total_tasks", "fps", "codebase_version")
        },
        "collection_summary": {
            key: summary.get(key)
            for key in (
                "schema_version",
                "complete_demonstrations",
                "target_layout",
                "task_instruction",
                "placement_order",
            )
        },
        "metadata": counts,
        "audit": audit(str(root), args.repo_id),
        "warnings": warnings(summary, counts, info),
    }
    if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
        compact = {
            "schema": 1,
            "status": "passed",
            "dataset_root": str(root),
            "episodes": counts.get("episodes"),
            "layouts": counts.get("layouts"),
        }
        Path.cwd().joinpath(REPORT).write_text(json.dumps(compact, sort_keys=True) + "\n")
        print(json.dumps(compact, sort_keys=True))
        return 0
    output_root = Path(os.environ.get("A3_OUTPUTS") or Path.cwd())
    relative = Path(args.output_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("--output-dir must stay below A3_OUTPUTS")
    target = (output_root / relative / OUTPUT).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"audit": str(target), "warnings": report["warnings"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
