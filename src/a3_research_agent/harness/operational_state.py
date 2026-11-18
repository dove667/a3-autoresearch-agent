"""Durable operational receipts, distinct from semantic or scientific acceptance."""

import codecs
import hashlib
import json
import os
import pathlib
import time

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as context_manager


def state_file():
    return state_paths.path(state_paths.OPERATIONS)


MAX_READS = 128


def load():
    if not state_file().exists():
        return {"version": 2, "revision": 0, "reads": {}, "jobs": {}, "counters": {}}
    state = json.loads(state_file().read_text())
    if (
        state.get("version") != 2
        or not isinstance(state.get("reads"), dict)
        or not isinstance(state.get("jobs"), dict)
    ):
        raise ValueError("Invalid operational state; preserve and inspect it")
    return state


def save(state):
    if state_file().exists():
        context_manager.atomic(str(state_file()) + ".previous", state_file().read_text())
    state["revision"] += 1
    context_manager.atomic(state_file(), json.dumps(state, ensure_ascii=False))


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def read_page(path, offset=0, limit=4000):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise ValueError("Page offset must be nonnegative; limit must be 1..8000 characters")
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    total, byte_count, pieces, binary = 0, 0, [], False
    with pathlib.Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        while True:
            chunk = stream.read(65536)
            digest.update(chunk)
            byte_count += len(chunk)
            if not binary:
                try:
                    text = decoder.decode(chunk, final=not chunk)
                except UnicodeDecodeError:
                    binary, pieces = True, []
                else:
                    left, right = max(0, offset - total), min(len(text), offset + limit - total)
                    if right > left:
                        pieces.append(text[left:right])
                    total += len(text)
            if not chunk:
                break
        after = os.fstat(stream.fileno())
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError("Source changed during paged read; retry with its current hash")
    if binary:
        return {
            "path": str(path),
            "sha256": digest.hexdigest(),
            "bytes": byte_count,
            "encoding": "binary",
            "notice": "Binary artifact; not UTF-8 text. Use an appropriate media or bounded processing tool.",
        }
    end = min(total, offset + limit)
    start = min(offset, total)
    page = {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "unit": "characters",
        "offset": start,
        "end": end,
        "total": total,
        "eof": end == total,
        "content": "".join(pieces),
    }

    # Include both JSON encodings and room for the receipt added after this read.
    def delivered_size():
        return context_manager.size(
            context_manager.output_message({**page, "receipt_id": "r" * 128}, "c" * 128)
        )

    while delivered_size() > 10000 and page["content"]:
        page["content"] = page["content"][: len(page["content"]) // 2]
        page["end"] = start + len(page["content"])
        page["eof"] = page["end"] == total
    return page


def visible_page_reference(page, messages):
    """Reuse an identical page only while the full original is in this model input."""
    if not isinstance(page, dict) or "content" not in page or "sha256" not in page:
        return None
    keys = ("path", "sha256", "offset", "end", "content")
    for message in reversed(messages):
        if message.get("type") != "function_call_output":
            continue
        raw = message.get("output")
        if not isinstance(raw, str) or len(raw) > 12000 or '"receipt_id"' not in raw:
            continue
        try:
            previous = json.loads(raw)
        except ValueError:
            continue
        if (
            isinstance(previous, dict)
            and previous.get("receipt_id") == message.get("call_id")
            and all(previous.get(key) == page[key] for key in keys)
        ):
            return {
                "status": "already_in_context",
                "previous_call_id": message["call_id"],
                **{key: page[key] for key in ("path", "sha256", "offset", "end", "total", "eof")},
            }
    return None


def record_page(call_id, page):
    if (
        not isinstance(page, dict)
        or not {"sha256", "path", "offset", "end", "total", "content"} <= page.keys()
    ):
        return
    state = load()
    page["receipt_id"] = call_id
    key = hashlib.sha256((page["path"] + "\0" + page["sha256"]).encode()).hexdigest()
    entry = state["reads"].setdefault(
        key,
        {
            "path": page["path"],
            "sha256": page["sha256"],
            "total": page["total"],
            "retrieved": [],
            "delivered": [],
            "receipts": {},
            "coverage_incomplete": False,
        },
    )
    payload_sha = hashlib.sha256(
        json.dumps(page, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    prior = entry["receipts"].get(call_id)
    if prior is not None:
        if prior["payload_sha256"] != payload_sha:
            raise ValueError("Read receipt call identity changed; preserve and inspect it")
        return  # A recovered read is not a second execution or a reopened delivery.
    counters = state["counters"]
    counters["read_calls"] = counters.get("read_calls", 0) + 1
    if any(a <= page["offset"] and b >= page["end"] for a, b in entry["retrieved"]):
        counters["repeated_reads"] = counters.get("repeated_reads", 0) + 1
    counters["retrieved_characters"] = counters.get("retrieved_characters", 0) + len(
        page["content"]
    )
    merged = merge_ranges(entry["retrieved"] + [[page["offset"], page["end"]]])
    entry["coverage_incomplete"] = entry.get("coverage_incomplete", False) or len(merged) > 64
    entry["retrieved"] = merged[-64:]
    entry["receipts"][call_id] = {
        "payload_sha256": payload_sha,
        "range": [page["offset"], page["end"]],
        "delivered": False,
    }
    while len(entry["receipts"]) > 32:
        del entry["receipts"][next(iter(entry["receipts"]))]
    entry["updated"] = state["revision"] + 1
    while sum(len(e["receipts"]) for e in state["reads"].values()) > 256:
        candidates = [e for e in state["reads"].values() if e["receipts"]]
        oldest = min(candidates, key=lambda e: e.get("updated", 0))
        del oldest["receipts"][next(iter(oldest["receipts"]))]
        counters["evicted_receipts"] = counters.get("evicted_receipts", 0) + 1

    while len(state["reads"]) > MAX_READS:
        oldest = min(state["reads"], key=lambda k: state["reads"][k]["updated"])
        del state["reads"][oldest]
        counters["evicted_read_sources"] = counters.get("evicted_read_sources", 0) + 1
    save(state)


def mark_delivered(messages):
    """Mark the read pages the model has now actually received."""
    state = load()
    changed = False
    for message in messages:
        if message.get("type") != "function_call_output":
            continue
        try:
            page = json.loads(message["output"])
        except (ValueError, TypeError):
            continue
        if not isinstance(page, dict) or page.get("receipt_id") != message.get("call_id"):
            continue
        digest = hashlib.sha256(
            json.dumps(page, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        for entry in state["reads"].values():
            receipt = entry["receipts"].get(message["call_id"])
            if receipt and not receipt["delivered"] and receipt["payload_sha256"] == digest:
                receipt["delivered"] = True
                merged = merge_ranges(entry["delivered"] + [receipt["range"]])
                entry["coverage_incomplete"] = (
                    entry.get("coverage_incomplete", False) or len(merged) > 64
                )
                entry["delivered"] = merged[-64:]
                state["counters"]["delivered_reads"] = (
                    state["counters"].get("delivered_reads", 0) + 1
                )
                changed = True
    if changed:
        save(state)


def view(state=None):
    state = state if state is not None else load()
    reads = sorted(state["reads"].values(), key=lambda entry: entry["updated"], reverse=True)
    value = {
        "notice": "Automatic read/job observations. Task decisions and progress are managed only in plan.",
        "revision": state["revision"],
        "source": str(state_file()),
        "reads": [
            {
                key: entry[key]
                for key in (
                    "path",
                    "sha256",
                    "total",
                    "retrieved",
                    "delivered",
                    "coverage_incomplete",
                )
            }
            for entry in reads[:8]
        ],
        "omitted_reads": max(0, len(reads) - 8),
    }
    while value["reads"] and context_manager.size(value) > 8000:
        value["reads"].pop()
        value["omitted_reads"] += 1
    return value


def counters():
    state = load()
    result = {
        k: min(1000000000, state["counters"].get(k, 0))
        for k in ["read_calls", "repeated_reads", "retrieved_characters", "delivered_reads"]
    }
    return result


def record_gateway(method, path, result):
    if not isinstance(result, dict):
        return
    job = result.get("job_id")
    if (
        method not in {"GET", "POST"}
        or not (path == "/jobs" or path.startswith("/jobs/"))
        or not isinstance(job, str)
        or len(job) != 32
        or not all(c in "0123456789abcdef" for c in job)
    ):
        return
    status = result.get("status")
    if status not in {
        "created",
        "pending",
        "queued",
        "running",
        "restarting",
        "completed",
        "cancelled",
        "failed",
    }:
        return
    state = load()
    if update_job(state, job, status):
        save(state)


def record_wait(result):
    if not isinstance(result, dict) or result.get("reason") != "state_changed":
        return
    state = load()
    changed = False
    for path, row in result.get("changes", {}).items():
        job = path.removeprefix("/jobs/")
        if job in state.get("jobs", {}) and row.get("status") in TERMINAL_JOB_STATES:
            changed |= update_job(state, job, row["status"])
    if changed:
        save(state)


TERMINAL_JOB_STATES = frozenset({"completed", "cancelled", "failed"})


def update_job(state, job, status):
    jobs = state.setdefault("jobs", {})
    previous = jobs.get(job, {}).get("status")
    if previous in TERMINAL_JOB_STATES:
        return False
    jobs[job] = {"status": status, "observed_at": time.time()}
    terminal = sorted(
        (key for key, record in jobs.items() if record["status"] in TERMINAL_JOB_STATES),
        key=lambda key: jobs[key]["observed_at"],
    )
    for key in terminal[: max(0, len(terminal) - 64)]:
        del jobs[key]
    return True


def delivered_before(page):
    """Report whether this exact page is already visible in the current context."""
    if not isinstance(page, dict) or not {"path", "sha256", "offset", "end"} <= page.keys():
        return False
    key = hashlib.sha256((page["path"] + "\0" + page["sha256"]).encode()).hexdigest()
    entry = load()["reads"].get(key)
    if not entry:
        return False
    return any(
        start <= page["offset"] and end >= page["end"] for start, end in entry.get("delivered", [])
    )


def reset_delivered():
    """Forget delivered pages after a compaction rewrote the visible history."""
    state = load()
    if not state["reads"]:
        return
    for entry in state["reads"].values():
        entry["delivered"] = []
        for receipt in entry["receipts"].values():
            receipt["delivered"] = False
    save(state)
