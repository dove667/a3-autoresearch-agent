"""Small owner-scoped observation tokens; polling never consumes a notification."""

import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(owner, *, is_main, manifest, feedback):
    feedback = [x for x in feedback if x["owner"] == owner]
    inputs = {key: manifest.get(key) for key in ("status", "revision", "generation", "digest")}
    stable = {
        "owner": owner,
        "is_main": is_main,
        "inputs": inputs,
        "feedback_digest": fingerprint([(x["feedback_id"], x["sha256"]) for x in feedback]),
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
        "pending": any(not x.get("forwarded_at") for x in feedback),
    }
