"""Source-bound fake summaries for transport and recovery tests."""

import json

from a3_research_agent.harness.context.summary import SECTIONS, source_text


def handoff(
    messages, text="Preserve the supplied research evidence and continue the pending work."
):
    value = {"version": 1, **{name: [] for name in SECTIONS}}
    quote = source_text(messages[0])[:80]
    value["next_actions"] = [{"text": text, "source": 1, "quote": quote}]
    return json.dumps(value, ensure_ascii=False)


def handoff_from_request(
    body, text="Preserve the supplied research evidence and continue the pending work."
):
    return handoff(json.loads(body["transcript"]), text)
