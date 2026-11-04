"""Worker coordination: common."""

import hashlib


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class HostRequestFailed(ValueError):
    def __init__(self, status=None):
        super().__init__("Host request failed; inspect existing state before retry")
        self.status = status


def checked(req, method, path, data=None):
    result = req(method, path, data)
    if not isinstance(result, dict):
        raise HostRequestFailed()
    if result.get("http_error"):
        raise HostRequestFailed(result["http_error"])
    return result


def is_main(req):
    own = checked(req, "GET", "/sessions/self")
    evolution = req("GET", "/evolution")
    if isinstance(evolution, dict) and evolution.get("http_error") == 403:
        # Ordinary workers are deliberately excluded from evolution state. A valid
        # self identity plus this specific Host denial identifies a non-main role.
        return False
    if not isinstance(evolution, dict) or evolution.get("http_error"):
        raise ValueError("Cannot verify current main identity")
    return bool(own.get("session_id")) and own["session_id"] == evolution.get("main_owner")
