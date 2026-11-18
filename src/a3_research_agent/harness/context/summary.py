"""Validate source-bound research handoffs before replacing working context."""

import json
import re

SECTIONS = {
    "goal_corrections": "Goals and corrections",
    "results": "Results and experimental conditions",
    "failures": "Failures and rejected approaches",
    "active_jobs": "Active jobs and uncertain operations",
    "open_questions": "Unresolved work",
    "next_actions": "Next actions",
    "sources": "Important sources",
}
VERSION = 1
NUMBER = re.compile(r"(?<![0-9.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?(?![0-9.])")
JOB_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)


class SummaryValidationError(ValueError):
    """The proposed handoff cannot be verified against its supplied sources."""


def source_text(message):
    """Expose both readable JSON fields and their full serialized representation."""
    chunks = [json.dumps(message, ensure_ascii=False)]

    def visit(value):
        if isinstance(value, str):
            chunks.append(value)
            try:
                nested = json.loads(value)
            except (ValueError, TypeError):
                return
            if isinstance(nested, (dict, list)):
                visit(nested)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(message)
    return "\n".join(chunks)


def validate(document, messages):
    """Check schema, exact quotations and numeric/identifier claims.

    Quotation matching establishes provenance. It cannot decide arbitrary prose
    entailment; original observations and the refreshed facts frame stay available.
    """
    if not isinstance(document, dict) or set(document) != {"version", *SECTIONS}:
        raise SummaryValidationError("summary_schema_fields")
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise SummaryValidationError("summary_schema_version")
    texts = {}
    count = 0
    for section in SECTIONS:
        if not isinstance(document[section], list):
            raise SummaryValidationError("summary_section_must_be_array:" + section)
        for item in document[section]:
            if not isinstance(item, dict) or set(item) != {"text", "source", "quote"}:
                raise SummaryValidationError("summary_entry_fields:" + section)
            text, index, quote = item["text"], item["source"], item["quote"]
            if not isinstance(text, str) or not text.strip():
                raise SummaryValidationError("summary_entry_text_empty:" + section)
            if type(index) is not int or not 1 <= index <= len(messages):
                raise SummaryValidationError("summary_source_index_invalid:" + section)
            if not isinstance(quote, str) or not quote.strip():
                raise SummaryValidationError("summary_quote_empty:" + section)
            if index not in texts:
                texts[index] = source_text(messages[index - 1])
            if quote not in texts[index]:
                raise SummaryValidationError("summary_quote_not_in_source:" + section)
            for pattern, label in ((NUMBER, "number"), (JOB_ID, "job_id")):
                if set(pattern.findall(text)) - set(pattern.findall(quote)):
                    raise SummaryValidationError("summary_unsupported_" + label + ":" + section)
            count += 1
    if not count:
        raise SummaryValidationError("summary_all_sections_empty")
    return {"version": VERSION, "entries": count, "cited_messages": len(texts)}


def parse(text, messages):
    try:
        document = json.loads(text)
    except (ValueError, TypeError) as error:
        raise SummaryValidationError("summary_invalid_json") from error
    report = validate(document, messages)
    return document, render(document), report


DEGRADED_NOTICE = "程序自动降级交接：模型摘要未通过校验，以下为原文摘录。"


def _quote(document, section, index, texts, quote):
    """Keep only quotes that really occur in their cited message."""
    if not isinstance(quote, str):
        return False
    quote = quote.strip()
    if not quote or quote not in texts[index]:
        return False
    document[section].append({"text": DEGRADED_NOTICE + quote, "source": index + 1, "quote": quote})
    return True


def _excerpt(raw, needle, limit):
    index = raw.find(needle)
    if index < 0:
        return None
    return raw[index : index + limit]


def _excerpt_job(raw, job, status, limit=240):
    """Prefer the occurrence that also shows the observed job status."""
    start = 0
    while True:
        index = raw.find(job, start)
        if index < 0:
            return _excerpt(raw, job, limit)
        window = raw[index : index + limit]
        if not status or status in window:
            return window
        start = index + 1


def _literal(value):
    return json.dumps(value, ensure_ascii=False)


def _operational_view(messages):
    """Find the newest per-decision state refresh, which carries plan and jobs."""
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content")
        if messages[index].get("role") != "user" or not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get("frame") == "CURRENT_STATE_REFRESH":
            return index, value
    return None, None


def _from_operational(document, index, value, texts):
    plan = value.get("research_plan") or {}
    facts = value.get("research_facts") or {}
    direction = plan.get("direction") or {}
    objective = direction.get("objective")
    if isinstance(objective, str) and objective.strip():
        _quote(
            document,
            "goal_corrections",
            index,
            texts,
            _excerpt(texts[index], _literal(objective), len(_literal(objective))),
        )
    steps = plan.get("steps") or {}
    for key in [*(plan.get("focus") or []), *(plan.get("ready") or [])][:3]:
        task = (steps.get(key) or {}).get("task")
        if isinstance(task, str) and task.strip():
            _quote(
                document,
                "next_actions",
                index,
                texts,
                _excerpt(texts[index], _literal(task), len(_literal(task))),
            )
    for job, status in list((facts.get("active_jobs") or {}).items())[:8]:
        _quote(
            document,
            "active_jobs",
            index,
            texts,
            _excerpt_job(texts[index], job, status if isinstance(status, str) else None),
        )
    for item in (plan.get("review_due") or [])[:3]:
        if isinstance(item, dict) and isinstance(item.get("step"), str):
            _quote(
                document, "open_questions", index, texts, _excerpt(texts[index], item["step"], 240)
            )


MEASURED_FIELDS = ("evaluation", "report", "metrics", "score", "results", "summary")
SECTION_ENTRIES = {"failures": 4, "results": 3, "active_jobs": 3, "sources": 2}


def _measured(value):
    """Report whether a finished job carries a result a reader can act on."""
    if not isinstance(value, dict) or value.get("status") not in {"completed", "failed"}:
        return False
    return any(key in value for key in MEASURED_FIELDS) or "exit_code" in value


def _tool_sections(messages, names):
    """Classify recent results by tool so a degraded handoff stays useful."""
    sections = {}
    for message in messages:
        if message.get("type") != "function_call_output":
            continue
        raw = message.get("output")
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = names.get(message.get("call_id"), "")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict) and (
            value.get("ok") is False or value.get("status") in {"failed", "cancelled"}
        ):
            section = "failures"
        elif _measured(value):
            # A finished job with measured values is a result, not queue traffic.
            section = "results"
        elif name in {"job_status", "job_wait", "job_list", "job_submit", "job_prepare"}:
            section = "active_jobs"
        elif name in {"read", "list", "search_files", "write", "edit"}:
            section = "sources"
        else:
            section = "results"
        sections.setdefault(section, []).append(message.get("call_id"))
    return sections


def degraded(messages, *, fits=None, quote_limit=200, entries=8):
    """Rebuild a verifiable handoff from its own sources without a model call.

    Used only when the model summary cannot be repaired: every entry is a verbatim
    excerpt of the cited message, so provenance still holds and research continues.
    """
    texts = [source_text(message) for message in messages]
    names = {
        message.get("call_id"): message.get("name")
        for message in messages
        if message.get("type") == "function_call"
    }
    document = {"version": VERSION, **{section: [] for section in SECTIONS}}
    index, value = _operational_view(messages)
    if value is not None:
        _from_operational(document, index, value, texts)
    sections = _tool_sections(messages, names)
    emitted = 0
    # Every section gets its own allowance: a run whose recent traffic is all job
    # polling must still hand over the measured results it did produce.
    for section in ("failures", "results", "active_jobs", "sources"):
        if emitted >= entries:
            break
        call_ids = set(sections.get(section, []))
        quota = min(SECTION_ENTRIES[section], entries - emitted)
        taken = 0
        for position in range(len(messages) - 1, -1, -1):
            if taken >= quota:
                break
            message = messages[position]
            if message.get("call_id") not in call_ids:
                continue
            raw = message.get("output")
            if not isinstance(raw, str):
                continue
            if _quote(document, section, position, texts, raw.strip()[:quote_limit]):
                emitted += 1
                taken += 1
    if not any(document[section] for section in SECTIONS):
        _quote(document, "sources", len(messages) - 1, texts, texts[-1][:quote_limit])
    rendered = render(document)

    def too_long():
        return bool(fits is not None and not fits(rendered))

    while too_long() and sum(len(document[section]) for section in SECTIONS) > 1:
        for section in reversed(list(SECTIONS)):
            if document[section]:
                document[section].pop()
                break
        rendered = render(document)
    while too_long() and any(
        len(item["quote"]) > 40 for section in SECTIONS for item in document[section]
    ):
        for section in SECTIONS:
            for item in document[section]:
                if len(item["quote"]) > 40:
                    item["quote"] = item["quote"][:40]
                    item["text"] = DEGRADED_NOTICE + item["quote"]
        rendered = render(document)
    return document, render(document)


def render(document):
    lines = ["Research handoff. Source numbers refer to the archived summary transcript."]
    for section, title in SECTIONS.items():
        lines.append("\n## " + title)
        entries = document[section]
        if not entries:
            lines.append("No supported item recorded in this handoff.")
        for item in entries:
            lines.append(
                f"- {item['text']} [source {item['source']}]\n  Exact source: {json.dumps(item['quote'], ensure_ascii=False)}"
            )
    return "\n".join(lines)
