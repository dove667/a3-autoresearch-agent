"""Fixed A3 benchmark launcher attached to every benchmark job.

The harness writes this file into the workspace and attaches it as
`benchmark_runner.py`, so a job runs one task with one budget and one report
path. It answers the CPU preflight (`A3_RESEARCH_PREFLIGHT=1`) and otherwise
forwards to the deployed benchmark entry.

Run it through the `benchmark` tool; it is not a research script.
"""

import argparse
import json
import os
import subprocess
import sys
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
    minimum = TASK_STEPS["2x10"] if (args.task == "two-boxes" and args.box_quota == 10) else TASK_STEPS[args.task]
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
            *(["--prompt-style", args.prompt_style] if requires_prompt(args) and args.prompt_style else []),
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
    output_root = Path(os.environ.get("A3_OUTPUTS") or Path.cwd())
    relative = Path(args.output_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("--output-dir must stay below A3_OUTPUTS")
    output = (output_root / relative / f"seed-{args.seed_start}.json").resolve()
    print(json.dumps({"benchmark": str(output), "max_steps": steps}), flush=True)
    if args.diagnostics:
        return record_benchmark(args, steps, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    return run_benchmark(command(args, steps, output, sys.executable))


def diagnostic_artifacts(directory, seed):
    episode = directory / f"seed_{seed}"
    return episode / "trace.jsonl", {
        camera: episode / f"{camera}.mp4" for camera in CAMERAS
    }


def record_benchmark(args, steps, output):
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
            staged.rename(destination)
        return exit_code


def finalize_diagnostics(args, staged, output):
    diagnostics = staged / "diagnostics"
    for seed in range(args.seed_start, args.seed_start + args.episodes):
        trace, videos = diagnostic_artifacts(diagnostics, seed)
        for artifact in (trace, *videos.values()):
            if not artifact.is_file() or not artifact.stat().st_size:
                raise SystemExit(f"missing or empty diagnostic artifact: {artifact.relative_to(staged)}")

    paths = {"output": str(output), "diagnostic_dir": str(output.parent / "diagnostics")}
    report = staged / output.name
    result = json.loads(report.read_text())
    result["run"].update(paths)
    report.write_text(json.dumps(result, indent=2) + "\n")
    manifest = diagnostics / "manifest.json"
    metadata = json.loads(manifest.read_text())
    metadata.update(paths)
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")


def run_benchmark(call):
    return subprocess.run(call["arguments"], cwd=call["cwd"], env=call["env"], check=False).returncode


def main(argv=None):
    args = parser().parse_args(argv)
    if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
        return preflight(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
