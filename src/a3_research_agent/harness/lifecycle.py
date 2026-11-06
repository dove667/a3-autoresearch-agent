"""Keep research in one main Agent and a single bounded successor trial."""

from urllib.parse import urlsplit

INSTRUCTIONS = (
    "Carry out Policy and Harness research yourself in this workspace. Implement, test, "
    "train, evaluate, inspect evidence and fix ordinary script failures without delegating. "
    "Submit independent remote jobs before waiting; use the unified episode executor. "
    "Preserve ongoing jobs, source references, memory and skills. Use /evolution/trials "
    "only for one complete Harness successor: the predecessor suspends research, the "
    "candidate checks the stated change and waits for adoption, then the same candidate "
    "continues as main. Do not create workers or parallel research branches. Judge repairs "
    "by their original purpose; do not add unnecessary audit gates. Never disclose credentials."
)


def self_status(req):
    value = req("GET", "/sessions/self")
    if not isinstance(value, dict) or value.get("http_error"):
        raise RuntimeError("Cannot verify current research identity; preserve state")
    return value


def should_retire(req):
    own = self_status(req)
    return own.get("has_been_main") is True and own.get("is_main") is False


def guard_gateway(req, method, path, body):
    method = method.upper()
    path = urlsplit(path).path.rstrip("/")
    if method == "POST" and path == "/sessions":
        raise PermissionError("Workers were removed; execute research in the current Agent")
    if method == "POST" and path != "/sessions/self/stop" and should_retire(req):
        raise PermissionError("Former main must save state and exit after handoff")
