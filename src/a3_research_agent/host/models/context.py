"""Validated token budgets with model-window and provider-framing headroom."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ContextBudget:
    main_context_tokens: int
    worker_context_tokens: int
    summary_context_tokens: int
    trigger_tokens: int = 400000
    target_tokens: int = 256000
    summary_input_tokens: int = 48000
    output_reserve_tokens: int = 8192
    provider_reserve_tokens: int = 4096

    def __post_init__(self):
        if any(
            type(value) is not int or not 0 < value <= 2000000 for value in asdict(self).values()
        ):
            raise ValueError("Context budgets require bounded positive integer token counts")
        available = (
            min(self.main_context_tokens, self.worker_context_tokens) * 9 // 10
            - self.output_reserve_tokens
        )
        if not self.target_tokens < self.trigger_tokens < available:
            raise ValueError("Trigger/target exceed the configured model window and headroom")
        if (
            not 8000
            <= self.summary_input_tokens
            < self.summary_context_tokens * 9 // 10 - self.output_reserve_tokens
        ):
            raise ValueError("Summary input limit exceeds the configured model window")


def read_context_budget(environment: Mapping[str, str]) -> dict | None:
    if not environment.get("A3_MODEL_CONTEXT_TOKENS"):
        return None
    main = int(environment["A3_MODEL_CONTEXT_TOKENS"])
    windows = {}
    for role in ("worker", "summary"):
        key = "A3_" + role.upper() + "_CONTEXT_TOKENS"
        if environment.get("A3_" + role.upper() + "_MODEL_NAME") and not environment.get(key):
            raise ValueError("Each alternate model requires a verified context window")
        windows[role + "_context_tokens"] = int(environment.get(key, main))
    options = {
        name: int(environment[key])
        for name, key in {
            "trigger_tokens": "A3_CONTEXT_TRIGGER_TOKENS",
            "target_tokens": "A3_CONTEXT_TARGET_TOKENS",
            "summary_input_tokens": "A3_SUMMARY_INPUT_TOKENS",
        }.items()
        if environment.get(key)
    }
    return asdict(ContextBudget(main_context_tokens=main, **windows, **options))
