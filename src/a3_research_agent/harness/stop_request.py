"""Stop a run at the end of a turn instead of interrupting a call in flight.

Ctrl-C raises inside whatever is executing, so the Host has to discard the call it
was in and treat its outcome as unknown. When the operator only wants the run to
finish, asking it to stop between turns costs nothing: the turn's tool results are
already saved, the checkpoint is current, and the next start resumes from a
consistent point.
"""

import json
import time

from a3_research_agent.harness import state_paths

FILE = "stop-request.json"


def path():
    return state_paths.path(FILE)


def request(reason):
    """Ask the running process to stop after its current turn."""
    value = {"reason": str(reason)[:200], "requested_at": time.time()}
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(value, ensure_ascii=False) + "\n")
    return value


def pending():
    """Read a pending request without consuming it."""
    file = path()
    if not file.is_file():
        return None
    try:
        value = json.loads(file.read_text())
    except (OSError, ValueError):
        return {"reason": "unreadable stop request", "requested_at": None}
    return value if isinstance(value, dict) else {"reason": "invalid stop request"}


def take():
    """Consume a pending request; the process must stop after this turn."""
    value = pending()
    if value is None:
        return None
    try:
        path().unlink()
    except OSError:
        pass
    return value
