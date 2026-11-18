"""Fixed A3 benchmark launcher attached to every benchmark job.

The harness writes this file into the workspace and attaches it as
`benchmark_runner.py`, so a job runs one task with one budget and one report
path. It answers the CPU preflight (`A3_RESEARCH_PREFLIGHT=1`) and otherwise
forwards to the deployed benchmark entry.

Run it through the `benchmark` tool; it is not a research script.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path
from tempfile import TemporaryDirectory

SCENARIO_TASKS = (
    "missing-columns",
    "partial-gaps",
    "cross-column",
    "tilted",
    "prefilled",
    "two-boxes",
)
TASK_STEPS = {"2x5": 500, "2x10": 3200, **dict.fromkeys(SCENARIO_TASKS, 1000)}
PROFILES = ("basic", "medium", "advanced")
REPORT = "a3-preflight.json"
CAMERAS = ("front", "left_wrist", "right_wrist")
TRACE_EVENT_EDGE = 6
COOKIE_GEOMETRY = re.compile(r"cookie_(\d+)(?:_|$)")
TARGET_SURFACE = re.compile(r"(?:spare_)?target_bin\w*_(?:floor|bottom|\w+_wall)$")
TRACE_PREDICATES = (
    "cookies_in_source",
    "cookies_in_target",
    "target_slot_occupancy",
    "target_touches_all_walls",
    "exact_2x5_fill",
    "count_fill",
    "success_criterion",
    "success",
    "success_hold_count",
    "required_success_hold_steps",
    "safety_reason",
    "unmet_success_conditions",
)
PROTOCOL_FIELDS = (
    "task",
    "policy",
    "checkpoint",
    "weights_kind",
    "weights_sha256",
    "profile",
    "device",
    "max_steps",
    "n_action_steps",
    "prompt_style",
    "source_column",
    "tilt_level",
    "box_quota",
    "inference_seed",
    "success_conditions",
    "randomization",
    "first_grasp",
    "git_commit",
)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--task", required=True, choices=sorted(TASK_STEPS))
    result.add_argument("--tilt-level", choices=("mild", "fallen"))
    result.add_argument("--box-quota", type=int, choices=(5, 10))
    result.add_argument("--policy", required=True, help="act:<checkpoint> or smolvla:<checkpoint>")
    result.add_argument("--profile", default="basic", choices=PROFILES)
    result.add_argument("--episodes", type=int, default=5)
    result.add_argument("--seed-start", type=int, default=10000)
    result.add_argument("--max-steps", type=int)
    result.add_argument("--n-action-steps", type=int, default=25)
    result.add_argument("--workers", type=int, default=1)
    # The old expert picks a column; a scenario or a policy should choose itself.
    result.add_argument("--source-column", default=None)
    result.add_argument("--prompt-style", help="Language-conditioned policies only")
    result.add_argument("--diagnostics", action="store_true")
    result.add_argument("--output-dir", required=True)
    return result


def requires_prompt(args):
    """Report whether this policy reads the task instruction at all."""
    return args.policy.split(":", 1)[0].strip().lower() != "act"


def budget(args):
    """Never run a task below the budget the A3 repository uses for it."""
    minimum = (
        TASK_STEPS["2x10"]
        if (args.task == "two-boxes" and args.box_quota == 10)
        else TASK_STEPS[args.task]
    )
    if args.max_steps is not None and args.max_steps < minimum:
        raise SystemExit(f"{args.task} needs at least {minimum} steps, not {args.max_steps}")
    return max(args.max_steps or minimum, minimum)


def checkpoint(root):
    """Accept a pretrained_model directory or a run directory that contains one."""
    candidate = Path(root)
    if not candidate.is_absolute():
        raise SystemExit(f"checkpoint path must be absolute: {root}")
    if (candidate / "pretrained_model").is_dir():
        candidate = candidate / "pretrained_model"
    if not candidate.is_dir():
        raise SystemExit(f"checkpoint directory missing: {candidate}")
    return candidate


def preflight(args):
    """Load the real checkpoint and package, then leave a compact passed report."""
    import a3_dual_arm_sim  # noqa: F401 - fail before a GPU job if the package is absent
    import torch  # noqa: F401 - the deploy path needs the real runtime
    from safetensors.torch import load_file

    if args.diagnostics:
        import cv2  # noqa: F401 - the deployed diagnostic recorder writes MP4

    directory = checkpoint(args.policy.split(":", 1)[-1])
    weights = sorted(directory.glob("*.safetensors"))
    if not weights:
        raise SystemExit(f"no safetensors weights under {directory}")
    state = load_file(str(weights[0]), device="cpu")
    first = next(iter(state.values())).float()
    report = {
        "schema": 1,
        "status": "passed",
        "task": args.task,
        "max_steps": budget(args),
        "checkpoint": str(directory),
        "weights": len(weights),
        "checksum": round(float(first.sum().item()), 6),
    }
    Path.cwd().joinpath(REPORT).write_text(json.dumps(report, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0


def command(args, steps, output, python):
    """Forward to the deployed benchmark entry with the task's own budget."""
    root = os.environ.get("A3_A3") or os.getcwd()
    diagnostics = Path(output).parent / "diagnostics"
    return {
        "arguments": [
            python,
            "-m",
            "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli",
            "--task",
            args.task,
            "--policy",
            args.policy,
            "--device",
            "cuda",
            "--profile",
            args.profile,
            "--episodes",
            str(args.episodes),
            "--seed-start",
            str(args.seed_start),
            "--max-steps",
            str(steps),
            "--workers",
            str(args.workers),
            *(["--source-column", str(args.source_column)] if args.source_column else []),
            *(["--tilt-level", args.tilt_level] if args.tilt_level else []),
            *(["--box-quota", str(args.box_quota)] if args.box_quota else []),
            *(
                ["--prompt-style", args.prompt_style]
                if requires_prompt(args) and args.prompt_style
                else []
            ),
            "--n-action-steps",
            str(args.n_action_steps),
            "--output",
            str(output),
            *(["--diagnostic-dir", str(diagnostics)] if args.diagnostics else []),
        ],
        "cwd": root,
        "env": {**os.environ, "MUJOCO_GL": "egl"},
    }


def run(args):
    steps = budget(args)
    output_root = Path(os.environ.get("A3_OUTPUTS") or Path.cwd()).resolve()
    relative = Path(args.output_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("--output-dir must stay below A3_OUTPUTS")
    output = (output_root / relative / f"seed-{args.seed_start}.json").resolve()
    print(json.dumps({"benchmark": str(output), "max_steps": steps}), flush=True)
    if args.diagnostics:
        return record_benchmark(args, steps, output, output_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    exit_code = run_benchmark(command(args, steps, output, sys.executable))
    if exit_code == 0:
        write_receipt(args, steps, output, output.relative_to(output_root))
    return exit_code


def receipt_path(report):
    return report.with_suffix(".receipt.json")


def diagnostic_artifacts(directory, seed):
    episode = directory / f"seed_{seed}"
    return episode / "trace.jsonl", {camera: episode / f"{camera}.mp4" for camera in CAMERAS}


def record_benchmark(args, steps, output, output_root):
    """Publish the report and recordings together after a successful process exit."""
    destination = output.parent
    if destination.exists():
        raise SystemExit(f"benchmark output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".benchmark-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        call = command(args, steps, staged / output.name, sys.executable)
        exit_code = run_benchmark(call)
        if exit_code == 0:
            finalize_diagnostics(args, staged, output)
            write_receipt(args, steps, staged / output.name, output.relative_to(output_root))
            staged.rename(destination)
        return exit_code


def finalize_diagnostics(args, staged, output):
    diagnostics = staged / "diagnostics"
    for seed in range(args.seed_start, args.seed_start + args.episodes):
        trace, videos = diagnostic_artifacts(diagnostics, seed)
        for artifact in (trace, *videos.values()):
            if not artifact.is_file() or not artifact.stat().st_size:
                raise SystemExit(
                    f"missing or empty diagnostic artifact: {artifact.relative_to(staged)}"
                )

    paths = {"output": str(output), "diagnostic_dir": str(output.parent / "diagnostics")}
    report = staged / output.name
    result = json.loads(report.read_text())
    result["run"].update(paths)
    report.write_text(json.dumps(result, indent=2) + "\n")
    manifest = diagnostics / "manifest.json"
    metadata = json.loads(manifest.read_text())
    metadata.update(paths)
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")


def box_state(after, box):
    count = after.get(f"cookies_in_{box}")
    mask = after.get(f"cookies_in_{box}_mask")
    members = (
        {index for index, present in enumerate(mask) if present} if isinstance(mask, list) else None
    )
    return count, members


def box_change(previous, current):
    before, old_members = previous
    count, members = current
    change = {"count": count} if count is not None and count != before else {}
    if members is not None and old_members is not None and members != old_members:
        if count is not None:
            change["count"] = count
        if entered := members - old_members:
            change["entered"] = sorted(entered)
        if exited := old_members - members:
            change["exited"] = sorted(exited)
    return change


def target_contacts(after):
    for contact in after.get("contacts") or []:
        geom1 = contact.get("geom1") or ""
        geom2 = contact.get("geom2") or ""
        for cookie_geom, surface in ((geom1, geom2), (geom2, geom1)):
            cookie = COOKIE_GEOMETRY.match(cookie_geom)
            if cookie and TARGET_SURFACE.fullmatch(surface):
                yield surface, int(cookie[1])


def bounded_events(events):
    if len(events) <= TRACE_EVENT_EDGE * 2:
        return events, 0
    return events[:TRACE_EVENT_EDGE] + events[-TRACE_EVENT_EDGE:], len(
        events
    ) - TRACE_EVENT_EDGE * 2


def trace_events(trace):
    """Scan one diagnostic trace for observed changes and each cookie's first box contact."""
    first_changes, last_changes = [], deque(maxlen=TRACE_EVENT_EDGE)
    change_count, first_contacts = 0, {}
    milestones, max_hold, initial, previous, after = {}, 0, None, {}, {}
    step = None
    with trace.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            step, after = record["step"], record["after"]
            state = {box: box_state(after, box) for box in ("source", "target")}
            if initial is None:
                initial = {"step": step, **{box: count for box, (count, _) in state.items()}}
            else:
                changes = {
                    box: change
                    for box in state
                    if (change := box_change(previous[box], state[box]))
                }
                if changes:
                    change_count += 1
                    change = {"step": step, **changes}
                    if len(first_changes) < TRACE_EVENT_EDGE:
                        first_changes.append(change)
                    else:
                        last_changes.append(change)
            previous = state
            for contact in target_contacts(after):
                first_contacts.setdefault(contact, step)
            for predicate in ("count_fill", "exact_2x5_fill", "success"):
                if after.get(predicate) is True:
                    milestones.setdefault(f"first_{predicate}_step", step)
            max_hold = max(max_hold, after.get("success_hold_count") or 0)

    contacts_by_step = defaultdict(list)
    for (surface, cookie), contact_step in first_contacts.items():
        contacts_by_step[contact_step, surface].append(cookie)
    contact_events = [
        {"step": contact_step, "surface": surface, "cookies": sorted(cookies)}
        for (contact_step, surface), cookies in sorted(contacts_by_step.items())
    ]
    back_wall = [event for event in contact_events if event["surface"].endswith("_back_wall")]
    other_surfaces = [
        event for event in contact_events if not event["surface"].endswith("_back_wall")
    ]
    back_wall, back_wall_omitted = bounded_events(back_wall)
    other_surfaces, others_omitted = bounded_events(other_surfaces)
    visible_contacts = sorted(
        back_wall + other_surfaces, key=lambda event: (event["step"], event["surface"])
    )
    return {
        "step_index_convention": "zero-based",
        "video_frame_timing": "Frame at index step shows the observation before that step's action.",
        "initial": initial,
        "changes": first_changes + list(last_changes),
        "changes_omitted": change_count - len(first_changes) - len(last_changes),
        "first_target_contacts": visible_contacts,
        "target_contacts_omitted": back_wall_omitted + others_omitted,
        "max_success_hold_count": max_hold,
        **milestones,
        "final": {"step": step, **{key: after[key] for key in TRACE_PREDICATES if key in after}},
    }


def write_receipt(args, steps, report, relative_report):
    result = json.loads(report.read_text(encoding="utf-8"))
    run = result["run"]
    protocol = {key: run[key] for key in PROTOCOL_FIELDS if key in run}
    protocol.update(
        seed_start=args.seed_start, episodes=args.episodes, diagnostics=args.diagnostics
    )
    protocol.setdefault("task", args.task)
    protocol.setdefault("profile", args.profile)
    protocol.setdefault("max_steps", steps)
    protocol.setdefault("n_action_steps", args.n_action_steps)
    if diff := run.get("git_diff"):
        protocol["git_diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
    for key in ("scenario_protocol", "effective_policy_config", "inference_protocol"):
        if value := run.get(key):
            encoded = json.dumps(value, sort_keys=True).encode()
            protocol[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()

    rows = []
    for episode in result["episodes"]:
        seed = episode["seed"]
        raw_score = episode.get("cookies_in_target", episode.get("score"))
        maximum = episode["max_score"]
        row = {
            "seed": seed,
            "score_raw": raw_score,
            "score_capped": min(raw_score, maximum),
            "max_score": maximum,
            "success": episode["success"],
            "steps": episode["steps"],
        }
        if args.diagnostics:
            trace, _ = diagnostic_artifacts(report.parent / "diagnostics", seed)
            relative_trace, relative_videos = diagnostic_artifacts(
                relative_report.parent / "diagnostics", seed
            )
            summary = trace_events(trace)
            events_path = relative_trace.with_name("events.json")
            trace.with_name("events.json").write_text(
                json.dumps(summary, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            row.update(
                trace=relative_trace.as_posix(),
                videos={camera: path.as_posix() for camera, path in relative_videos.items()},
                events=events_path.as_posix(),
                trace_events=summary,
            )
        rows.append(row)
    receipt = {
        "schema": 1,
        "report": relative_report.as_posix(),
        "protocol": protocol,
        "episodes": rows,
    }
    receipt_path(report).write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def run_benchmark(call):
    return subprocess.run(
        call["arguments"], cwd=call["cwd"], env=call["env"], check=False
    ).returncode


def main(argv=None):
    args = parser().parse_args(argv)
    if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
        return preflight(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
