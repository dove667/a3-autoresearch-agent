"""Restore the saved mini context without reconstructing it through a new prompt."""

import json
import os
from copy import deepcopy
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow


def load_context(path: Path) -> dict:
    context = json.loads(path.read_text())
    messages = deepcopy(context.get("messages", []))
    if not messages or messages[0].get("role") != "system":
        raise ValueError("Checkpoint has no original system context")
    if messages[-1].get("role") == "exit":
        status = messages[-1].get("extra", {}).get("exit_status")
        if status == "Submitted":
            raise ValueError("A submitted session cannot be resumed; select its retained version")
        messages.pop()  # The runtime failure marker is not a model-context message.
    pending = set()
    for message in messages:
        for item in message.get("output", []) if isinstance(message.get("output"), list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                pending.add(item["call_id"])
        if message.get("type") == "function_call_output":
            pending.discard(message.get("call_id"))
    if pending or (messages and messages[-1].get("extra", {}).get("actions")):
        raise ValueError("Unresolved tool execution: reconcile its result before resuming")
    context["messages"] = messages
    return context


class ContextAgent(DefaultAgent):
    """Keep upstream fresh-run behavior; add explicit saved-context execution."""

    def save(self, path: Path | None, *extra_dicts) -> dict:
        data = super().save(None, *extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                json.dump(data, stream, ensure_ascii=False)
            temporary.replace(path)
        return data

    def resume(self, context: dict) -> dict:
        self.messages = deepcopy(context["messages"])
        stats = context.get("info", {}).get("model_stats", {})
        self.n_calls = stats.get("api_calls", 0)
        self.cost = stats.get("instance_cost", 0.0)
        # Match the pinned upstream run loop without reinitializing system/user messages.
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as error:
                self.cost += error.messages[0].get("extra", {}).get("cost", 0.0)
                self.n_consecutive_format_errors += 1
                self.add_messages(*error.messages)
                if (
                    self.config.max_consecutive_format_errors > 0
                    and self.n_consecutive_format_errors
                    >= self.config.max_consecutive_format_errors
                ):
                    self.add_messages(
                        {
                            "role": "exit",
                            "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                        }
                    )
            except InterruptAgentFlow as error:
                self.add_messages(*error.messages)
            except Exception as error:
                self.handle_uncaught_exception(error)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                return self.messages[-1].get("extra", {})
