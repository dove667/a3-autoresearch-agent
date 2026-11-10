"""Keep research in one main Agent and a single bounded successor trial."""

from urllib.parse import urlsplit

INSTRUCTIONS = (
    "Run the assigned research as one Agent. Submit independent jobs before waiting. "
    "Use the current research protocol and carry findings forward with evidence. "
    "Report Harness and infrastructure faults to the operator."
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
