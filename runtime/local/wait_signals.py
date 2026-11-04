"""Small owner-scoped observation tokens; polling never consumes a notification."""

import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(owner, *, is_main, manifest, feedback, analysis):
    feedback = [x for x in feedback if x["owner"] == owner]
    analysis = [x for x in analysis if x["owner"] == owner]
    terminal = [x for x in analysis if x.get("status") in {"completed", "failed", "interrupted"}]
    latest = terminal[-1] if terminal else {}
    pending = [
        x["analysis_id"]
        for x in terminal
        if x.get("status") == "completed" and not x.get("response")
    ]
    inputs = {key: manifest.get(key) for key in ("status", "revision", "generation", "digest")}
    stable = {
        "owner": owner,
        "is_main": is_main,
        "inputs": inputs,
        "feedback_digest": fingerprint([(x["feedback_id"], x["sha256"]) for x in feedback]),
        "analysis_terminal": {key: latest.get(key) for key in ("analysis_id", "status")},
    }
    return {
        "schema": 1,
        "cursor": fingerprint(stable),
        "is_main": is_main,
        "inputs": inputs,
        "feedback": {
            "count": len(feedback),
            "latest_id": feedback[-1]["feedback_id"] if feedback else None,
        },
        "analysis": {
            **stable["analysis_terminal"],
            "pending_ids": pending[:8],
            "omitted_pending": max(0, len(pending) - 8),
        },
        "pending": bool(pending)
        or bool(latest.get("status") in {"failed", "interrupted"} and not latest.get("response"))
        or any(not x.get("forwarded_at") for x in feedback),
    }
