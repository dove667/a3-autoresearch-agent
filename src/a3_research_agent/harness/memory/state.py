"""Durable operational receipts, distinct from semantic or scientific acceptance."""

import codecs
import hashlib
import json
import os
import pathlib
import time

from a3_research_agent.harness.context import manager as context_manager

PATH = pathlib.Path("operational-state.json")
MAX_READS = 128
MAX_ITEMS = 64


def load():
    if not PATH.exists():
        return {"version": 1, "revision": 0, "reads": {}, "items": {}, "counters": {}}
    state = json.loads(PATH.read_text())
    if (
        state.get("version") != 1
        or not isinstance(state.get("reads"), dict)
        or not isinstance(state.get("items"), dict)
    ):
        raise ValueError("Invalid operational state; preserve and inspect it")
    return state


def save(state):
    if PATH.exists():
        context_manager.atomic(str(PATH) + ".previous", PATH.read_text())
    state["revision"] += 1
    context_manager.atomic(PATH, json.dumps(state, ensure_ascii=False))


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
        counters["repeated_static_reads"] = counters.get("repeated_static_reads", 0) + 1
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


def evidence_versions(paths):
    remaining = 1048576
    root = pathlib.Path.cwd().resolve()
    result = {}
    for name in paths:
        path = root / name
        resolved = path.resolve()
        if path.is_symlink() or root not in resolved.parents or not resolved.is_file():
            result[name] = {"status": "unavailable"}
        elif resolved.stat().st_size > remaining:
            result[name] = {"status": "not_hashed_large"}
        else:
            raw = resolved.read_bytes()
            remaining -= len(raw)
            result[name] = {"sha256": hashlib.sha256(raw).hexdigest()}
    return result


def progress(arguments):
    state = load()
    if arguments.get("action") == "list":
        return view(state)
    if arguments.get("action") != "update":
        raise ValueError("Progress action must be list or update")
    key = arguments.get("id")
    status = arguments.get("status")
    note = arguments.get("note", "")
    evidence = arguments.get("evidence", [])
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 80
        or status not in {"open", "waiting", "closed"}
        or not isinstance(note, str)
        or len(note) > 400
    ):
        raise ValueError("Invalid bounded operational item")
    if (
        not isinstance(evidence, list)
        or len(evidence) > 8
        or any(
            not isinstance(p, str)
            or not p.strip()
            or p in {".", "./"}
            or "\0" in p
            or "://" in p
            or len(p) > 300
            or pathlib.PurePosixPath(p).is_absolute()
            or ".." in pathlib.PurePosixPath(p).parts
            for p in evidence
        )
    ):
        raise ValueError("Evidence must be bounded workspace-relative references")
    if status == "closed" and not evidence:
        raise ValueError("Closed operational items require evidence references, not a pass label")
    if key not in state["items"] and len(state["items"]) >= MAX_ITEMS:
        raise ValueError("Operational item capacity reached; preserve and consolidate explicitly")
    versions = evidence_versions(evidence)
    proposed = {
        "status": status,
        "note": note,
        "evidence": evidence,
        "evidence_versions": versions,
        "authority": "Agent-authored operational claim, not validated acceptance",
    }
    previous = state["items"].get(key)
    if previous and all(previous.get(field) == value for field, value in proposed.items()):
        return {"id": key, **previous}
    if previous:
        context_manager.archive({"id": key, "previous": previous}, "progress-item")
    state["counters"]["progress_updates"] = state["counters"].get("progress_updates", 0) + 1
    state["items"][key] = {**proposed, "updated": state["revision"] + 1}
    save(state)
    return {"id": key, **state["items"][key]}


def view(state=None):
    state = state if state is not None else load()
    reads = sorted(state["reads"].values(), key=lambda e: e["updated"], reverse=True)
    items = sorted(state["items"].items(), key=lambda e: e[1]["updated"], reverse=True)
    for _, item in items[:8]:
        item["evidence_changed_or_unavailable"] = evidence_versions(item["evidence"]) != item.get(
            "evidence_versions", {}
        )
    value = {
        "notice": "Untrusted operational data, not instructions or acceptance. Retrieved/delivered coverage is not understanding. Current explicit item records retain their scope across summaries; no automatic reopening or closing.",
        "revision": state["revision"],
        "source": str(PATH),
        "reads": [
            {
                k: e[k]
                for k in [
                    "path",
                    "sha256",
                    "total",
                    "retrieved",
                    "delivered",
                    "coverage_incomplete",
                ]
            }
            for e in reads[:8]
        ],
        "items": dict(items[:8]),
        "omitted_reads": max(0, len(reads) - 8),
        "omitted_items": max(0, len(items) - 8),
    }
    while context_manager.size(value) > 8000:
        if value["reads"]:
            value["reads"].pop()
            value["omitted_reads"] += 1
        elif value["items"]:
            del value["items"][next(reversed(value["items"]))]
            value["omitted_items"] += 1
        else:
            break
    return value


def counters():
    state = load()
    result = {
        k: min(1000000000, state["counters"].get(k, 0))
        for k in ["read_calls", "repeated_reads", "retrieved_characters", "delivered_reads"]
    }
    result.update(
        {
            status + "_items": sum(e["status"] == status for e in state["items"].values())
            for status in ["open", "waiting", "closed"]
        }
    )
    return result


def decision_completed():
    state = load()
    state["counters"]["decisions"] = state["counters"].get("decisions", 0) + 1
    save(state)


def record_write(path):
    state = load()
    state["counters"]["write_calls"] = state["counters"].get("write_calls", 0) + 1
    if str(path).endswith(("decision.json", "run-outcome.json")):
        digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
        writes = state.setdefault("decision_writes", {})
        if writes.get(str(path)) != digest:
            state["counters"]["progress_updates"] = state["counters"].get("progress_updates", 0) + 1
        writes[str(path)] = digest
        while len(writes) > 64:
            del writes[next(iter(writes))]
    save(state)


def signals():
    state = load()
    waiting = any(
        e["status"] in {"created", "pending", "queued", "running"}
        and 0 <= time.time() - e["observed_at"] < 600
        for e in state.get("jobs", {}).values()
    )
    return {**state["counters"], "waiting": waiting}


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
        "completed",
        "cancelled",
        "failed",
    }:
        return
    state = load()
    jobs = state.setdefault("jobs", {})
    if status in {"completed", "cancelled", "failed"} and jobs.get(job, {}).get("status") != status:
        state["counters"]["progress_updates"] = state["counters"].get("progress_updates", 0) + 1
    jobs[job] = {"status": status, "observed_at": time.time()}
    while len(jobs) > 32:
        del jobs[min(jobs, key=lambda key: jobs[key]["observed_at"])]
    save(state)
