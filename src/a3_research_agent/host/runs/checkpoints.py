"""Validate complete tool boundaries in saved model transcripts."""


def require_tool_results(messages: list[dict]) -> None:
    pending = set()
    for message in messages:
        output = message.get("output")
        items = output if isinstance(output, list) else [message]
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call":
                pending.add(item["call_id"])
        if message.get("type") == "function_call_output":
            pending.discard(message.get("call_id"))
    if pending or (messages and messages[-1].get("extra", {}).get("actions")):
        raise ValueError("Unresolved tool execution: reconcile its result before resuming")
