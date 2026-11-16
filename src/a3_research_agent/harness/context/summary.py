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
