"""Name the failure a finished job actually shows, so the next action is obvious.

The Harness reads only the log tail the Controller already returned; it never
guesses a cause that the text does not support.
"""

RULES = (
    ("dependency_missing", ("ModuleNotFoundError", "ImportError", "No module named")),
    ("gpu_oom", ("CUDA out of memory", "OutOfMemoryError", "CUDA_ERROR_OUT_OF_MEMORY")),
    ("storage_full", ("No space left on device", "Disk quota exceeded")),
    ("permission_denied", ("PermissionError", "EACCES", "Read-only file system")),
    ("missing_input", ("FileNotFoundError", "No such file or directory")),
    ("distributed_failure", ("NCCL", "nccl", "torch.distributed", "Address already in use")),
    ("killed", ("Killed", "SIGKILL", "terminated by signal")),
    ("assertion_failed", ("AssertionError",)),
    ("preflight_unimplemented", ("a3-preflight.json",)),
)
ACTIONS = {
    "dependency_missing": "Attach or install the missing module, then prepare the request again.",
    "gpu_oom": "Lower batch or model size, or raise gpu_memory_mb, then resubmit.",
    "storage_full": "Remove unreferenced outputs under your own job or shrink the artifacts.",
    "permission_denied": "Use an authorized path or export; report a real permission gap.",
    "missing_input": "Check the attachment destinations and data paths against the real job layout.",
    "distributed_failure": "Reuse a single free port per job and verify CUDA/NCCL setup.",
    "killed": "The host or scheduler stopped the process; check memory use and time limits.",
    "assertion_failed": "Read the assertion and the values it compared before changing anything.",
    "preflight_unimplemented": "Add the A3_RESEARCH_PREFLIGHT branch that writes a3-preflight.json.",
}
ERROR_LINE = ("Error", "Exception", "error:", "ERROR", "Traceback")
# Only a failure that says nothing about the script may be repeated unchanged.
# Everything else would run the same wrong code again and spend the same GPU time.
RETRYABLE = frozenset({"killed", "distributed_failure"})
RETRY_REASONS = {
    "killed": "The scheduler or host stopped the process; the same request may finish next time.",
    "distributed_failure": "A port or NCCL setup collided; the same request may succeed alone.",
}


def _tail(result):
    output = result.get("output")
    if isinstance(output, str):
        return output
    tail = result.get("log_tail")
    return tail if isinstance(tail, str) else ""


def classify(text):
    """Return (failure_class, error_line) for the first matching rule."""
    if not isinstance(text, str) or not text.strip():
        return None, None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    error_line = next(
        (line for line in reversed(lines) if any(marker in line for marker in ERROR_LINE)),
        lines[-1],
    )
    for name, markers in RULES:
        if any(marker in text for marker in markers):
            return name, error_line[:240]
    return None, error_line[:240]


def diagnose(result):
    """Attach a bounded diagnosis to a finished job observation, when one applies."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    exit_code = result.get("exit_code")
    if status not in {"failed", "cancelled"} and not (
        status == "completed" and type(exit_code) is int and exit_code != 0
    ):
        return None
    name, line = classify(_tail(result))
    if name is None:
        return {
            "failure_class": "unclassified",
            "error_line": line,
            "suggested_action": "Read the full log and the artifacts before changing the script.",
            "retry": {"resubmit": False, "reason": "The cause is unknown; read the log first."},
        }
    return {
        "failure_class": name,
        "error_line": line,
        "suggested_action": ACTIONS[name],
        "retry": retry_advice(name),
    }


def retry_advice(name):
    """Say whether resubmitting the identical request is defensible."""
    if name in RETRYABLE:
        return {"resubmit": True, "max_attempts": 1, "reason": RETRY_REASONS[name]}
    return {
        "resubmit": False,
        "reason": "This failure comes from the request or the code; repeating it reproduces it.",
    }
