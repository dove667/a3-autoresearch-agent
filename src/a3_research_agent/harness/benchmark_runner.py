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

TASK_STEPS = {"2x5": 500, "2x10": 3200}
PROFILES = ("basic", "medium", "advanced")
REPORT = "a3-preflight.json"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--task", required=True, choices=sorted(TASK_STEPS))
    result.add_argument("--policy", required=True, help="act:<checkpoint> or smolvla:<checkpoint>")
    result.add_argument("--profile", default="basic", choices=PROFILES)
    result.add_argument("--episodes", type=int, default=5)
    result.add_argument("--seed-start", type=int, default=10000)
    result.add_argument("--max-steps", type=int)
    result.add_argument("--n-action-steps", type=int, default=25)
    result.add_argument("--workers", type=int, default=1)
    result.add_argument("--source-column", type=int, default=1)
    result.add_argument("--prompt-style", help="Language-conditioned policies only")
    result.add_argument("--output-dir", required=True)
    return result


def requires_prompt(args):
    """Report whether this policy reads the task instruction at all."""
    return args.policy.split(":", 1)[0].strip().lower() != "act"


def budget(args):
    """Never run a task below the budget the A3 repository uses for it."""
    minimum = TASK_STEPS[args.task]
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
            "--source-column",
            str(args.source_column),
            *(["--prompt-style", args.prompt_style] if requires_prompt(args) and args.prompt_style else []),
            "--n-action-steps",
            str(args.n_action_steps),
            "--output",
            str(output),
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
    output.parent.mkdir(parents=True, exist_ok=True)
    call = command(args, steps, output, sys.executable)
    print(json.dumps({"benchmark": str(output), "max_steps": steps}), flush=True)
    return subprocess.run(call["arguments"], cwd=call["cwd"], env=call["env"], check=False).returncode


def main(argv=None):
    args = parser().parse_args(argv)
    if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
        return preflight(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
