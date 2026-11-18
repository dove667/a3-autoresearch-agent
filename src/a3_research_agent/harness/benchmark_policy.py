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

# The six extension scenarios are 2x5-scale tasks: the upstream guide runs them
# with --max-steps 1000. Two full target boxes hold 20 cookies and follow 2x10.
SCENARIO_TASKS = (
    "missing-columns",
    "partial-gaps",
    "cross-column",
    "tilted",
    "prefilled",
    "two-boxes",
)
TASK_STEPS = {
    "2x5": 500,
    "2x10": 3200,
    **dict.fromkeys(SCENARIO_TASKS, 1000),
    # two-boxes with --box-quota 10 is resolved in steps_for(), not here.
}
# A scenario scores against its own target, so its report can never reach the 2x5
# maximum. The benchmark report uses this to read "in box" against the right number.
MAX_SCORE = {
    "2x5": 10,
    "2x10": 20,
    "missing-columns": 10,
    "partial-gaps": 10,
    "cross-column": 10,
    "tilted": 10,
    "prefilled": 5,
    "two-boxes": 10,
}


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
    return task if task in MAX_SCORE else None


def steps_for(task, values):
    """Return the step budget for a task, raising it for a twenty-cookie two-boxes run."""
    if task == "two-boxes" and option(values, "--box-quota") == "10":
        return TASK_STEPS["2x10"]
    return TASK_STEPS[task]


def max_score(task, values=None):
    """Return the best in-box count this task's report can show."""
    if task == "two-boxes" and values and option(values, "--box-quota") == "10":
        return 20
    return MAX_SCORE.get(task, 10)


def steps_of(values):
    raw = option(values, "--max-steps")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        fail("invalid_max_steps", "Pass --max-steps as an integer, for example 3200.")


def budget(task, steps=None, minimum=None):
    """Return the step budget to run, rejecting an explicitly too-small one.

    Omitting `--max-steps` is fine for the benchmark tool because the tool always
    writes the task budget. A command that reaches the benchmark entry without the
    option would use the entry's own 1000-step default, so `check_command` treats
    the missing option as a failure.
    """
    minimum = minimum or TASK_STEPS[task]
    if steps is None:
        return minimum
    if steps < minimum:
        fail("benchmark_budget_too_small", budget_hint(task, steps))
    return steps


def budget_hint(task, steps, minimum=None):
    minimum = minimum or TASK_STEPS[task]
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


def policy_of(values):
    """Read the --policy family: act consumes pixels and state, not language."""
    raw = (option(values, "--policy") or "").strip().lower()
    return raw.split(":", 1)[0] if raw else ""


def check_prompt_style(values):
    """Reject a language option for a policy that has no language input."""
    if policy_of(values) != "act" or option(values, "--prompt-style") is None:
        return
    fail(
        "prompt_style_not_applicable",
        "ACT reads three cameras and 16 state values; it has no language input, so "
        "--prompt-style cannot change its behaviour. Drop the option for act policies "
        "and keep it for smolvla.",
    )


def check_command(command):
    """Reject a benchmark command whose step budget cannot finish the task."""
    values = tokens(command)
    check_prompt_style(values)
    task = task_of(values)
    if task is None:
        return
    minimum = steps_for(task, values)
    steps = steps_of(values)
    if steps is not None and steps >= minimum:
        return
    fail("benchmark_budget_too_small", budget_hint(task, steps, minimum))
