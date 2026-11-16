"""Select bounded research facts before trimming a tool result's free-form text."""

import math
import re
from itertools import chain

RECEIPT_FIELDS = (
    "ok",
    "job_id",
    "prepared_id",
    "preflight_job_id",
    "export_id",
    "session_id",
    "status",
    "exit_code",
    "error",
    "hint",
    "reason",
    "sha256",
    "path",
    "source_path",
    "error_preview",
    "hint_preview",
    "reason_preview",
)

METRIC_WORDS = re.compile(
    r"(?:accuracy|success|pass_rate|loss|reward|score|metric|count|episodes|samples|"
    r"dataset|checkpoint|manifest|baseline|candidate|seed|condition|error|failure|failed)",
    re.IGNORECASE,
)
PRIVATE_WORDS = re.compile(
    r"(?:token|secret|password|credential|authorization|api_key)", re.IGNORECASE
)
MAX_VISITS = 512
MAX_FACTS = 48
MAX_DEPTH = 5


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if not isinstance(value, float) or math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= 300:
        return value
    return None


def select_facts(value):
    """Return exact small values with JSON-style locators and explicit omissions.

    Receipts have priority over metric details. Large strings, opaque payloads and
    credential-shaped fields remain in the verified original, never copied into
    this semantic view. Traversal and delivery have independent bounds.
    """
    if not isinstance(value, (dict, list)):
        return {}, 0, False
    facts = {}
    if isinstance(value, dict):
        for key in RECEIPT_FIELDS:
            item = value.get(key)
            scalar = _scalar(item)
            if key in value and (scalar is not None or item is None):
                facts[key] = scalar
    visits, omitted, complete = 0, 0, True

    def visit(container, prefix, depth):
        nonlocal visits, omitted, complete
        if isinstance(container, dict):
            # Inspect named research fields first, including when they occur after
            # a large body of unrelated fields in the original response.
            priority = [
                key for key in container if key in RECEIPT_FIELDS or METRIC_WORDS.search(str(key))
            ]
            selected_keys = set(priority)
            keys = chain(priority, (key for key in container if key not in selected_keys))
            children = ((key, container[key]) for key in keys)
        else:
            children = enumerate(container)
        for key, item in children:
            if visits >= MAX_VISITS:
                complete = False
                return
            visits += 1
            key = str(key)
            if PRIVATE_WORDS.search(key) or key in {"data", "content", "preview", "log", "logs"}:
                continue
            if len(key) > 100:
                complete = False
                continue
            path = prefix + ("." if prefix else "") + key
            if isinstance(item, (dict, list)):
                if depth < MAX_DEPTH:
                    visit(item, path, depth + 1)
                else:
                    complete = False
                continue
            scalar = _scalar(item)
            selected = key in RECEIPT_FIELDS or METRIC_WORDS.search(path)
            if selected and path not in facts:
                if scalar is None and item is not None:
                    if (
                        key in {"error", "hint", "reason"}
                        and isinstance(item, str)
                        and len(facts) + 2 <= MAX_FACTS
                    ):
                        facts[path + "_preview"] = item[:200]
                        facts[path + "_characters"] = len(item)
                    omitted += 1
                elif len(facts) < MAX_FACTS:
                    facts[path] = scalar
                else:
                    omitted += 1

    visit(value, "", 0)
    return facts, omitted, complete


def trim_optional_fact(facts):
    """Remove one least-priority detail while preserving top-level receipts."""
    for key in reversed(facts):
        if key not in RECEIPT_FIELDS:
            del facts[key]
            return True
    return False
