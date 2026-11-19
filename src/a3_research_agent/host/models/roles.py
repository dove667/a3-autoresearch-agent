"""Display names for current and historical model usage records."""

AGENT_NAMES = {
    "main": "Main Agent",
    "vision": "Visual Assistant Agent",
    "summary": "Compressor Agent",
    "research": "Deep Research Agent",
}


def agent_role(entry):
    purpose = entry.get("model_purpose")
    if purpose in ("vision", "research"):
        return purpose
    return entry.get("model_role") or "main"
