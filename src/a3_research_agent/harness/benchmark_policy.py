"""Keep every A3 evaluation on a step budget the task can actually finish in.

The deployed benchmark entry (`examples/benchmark_cookie_batch.py`) takes one
`--max-steps` for both tasks and defaults to 1000, while the task-specific
wrappers in the A3 repository use 500 for 2x5 and 3200 for 2x10. A 2x10 episode
that moves five cookies per row needs about 45 steps per cookie, so a short
budget caps the score and makes the native pass unreachable: 0/60 pass at 500
steps says nothing about the policy. Reject such a request at preparation time
instead of discovering it after the episode reports arrive.
"""

import shlex

from .tool_contracts import fail

TASK_STEPS = {"2x5": 500, "2x10": 3200}


def tokens(command):
    """Split a command array the way the remote shell will see its options."""
    result = []
    for part in command:
        try:
            result.extend(shlex.split(part))
        except ValueError:
            result.extend(part.split())
    return result


def option(values, flag):
    for index, value in enumerate(values):
        if value == flag and index + 1 < len(values):
            return values[index + 1]
        if value.startswith(flag + "="):
            return value.split("=", 1)[1]
    return None


def task_of(values):
    """Read --task in the forms the A3 entry points accept (2x10 or 2×10)."""
    task = (option(values, "--task") or "").strip().lower().replace("×", "x")
    return task if task in TASK_STEPS else None


def steps_of(values):
    raw = option(values, "--max-steps")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        fail("invalid_max_steps", "Pass --max-steps as an integer, for example 3200.")


def budget(task, steps=None):
    """Return the step budget to run, rejecting an explicitly too-small one.

    Omitting `--max-steps` is fine for the benchmark tool because the tool always
    writes the task budget. A command that reaches the benchmark entry without the
    option would use the entry's own 1000-step default, so `check_command` treats
    the missing option as a failure.
    """
    minimum = TASK_STEPS[task]
    if steps is None:
        return minimum
    if steps < minimum:
        fail("benchmark_budget_too_small", budget_hint(task, steps))
    return steps


def budget_hint(task, steps):
    minimum = TASK_STEPS[task]
    reason = (
        "the benchmark entry defaults to 1000 steps when --max-steps is omitted"
        if steps is None
        else f"{steps} steps is below the {minimum}-step task minimum"
    )
    return (
        f"{task} needs --max-steps {minimum} (or more); {reason}. A shorter budget cannot "
        f"finish the task, so pass/fail and in-box counts are not comparable. "
        f"Pass --max-steps {minimum}, or use the benchmark tool, which always sets "
        f"{minimum} for {task}."
    )


def check_command(command):
    """Reject a benchmark command whose step budget cannot finish the task."""
    values = tokens(command)
    task = task_of(values)
    if task is None:
        return
    steps = steps_of(values)
    if steps is not None and steps >= TASK_STEPS[task]:
        return
    fail("benchmark_budget_too_small", budget_hint(task, steps))
