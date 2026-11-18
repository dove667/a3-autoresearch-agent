"""Validated token budgets with model-window and provider-framing headroom."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ContextBudget:
    main_context_tokens: int
    worker_context_tokens: int
    summary_context_tokens: int
    trigger_tokens: int = 400000
    target_tokens: int = 32000
    output_reserve_tokens: int = 8192
    provider_reserve_tokens: int = 4096
    main_max_output_tokens: int | None = None
    worker_max_output_tokens: int | None = None
    summary_max_output_tokens: int | None = None

    def __post_init__(self):
        settings = asdict(self)
        if any(
            type(value) is not int or not 0 < value <= 2000000
            for name, value in settings.items()
            if not name.endswith("_max_output_tokens")
        ):
            raise ValueError("Context budgets require bounded positive integer token counts")
        if any(
            type(value) is not int or value <= 0
            for name, value in settings.items()
            if name.endswith("_max_output_tokens") and value is not None
        ):
            raise ValueError("Model output maxima must be positive integer token counts")
        available = (
            min(self.main_context_tokens, self.worker_context_tokens) * 9 // 10
            - self.output_reserve_tokens
        )
        if not self.target_tokens < self.trigger_tokens < available:
            raise ValueError("Trigger/target exceed the configured model window and headroom")
        if self.trigger_tokens >= self.summary_input_tokens - self.provider_reserve_tokens:
            raise ValueError("Summary model window cannot contain the full trigger transcript")

    @property
    def summary_input_tokens(self):
        return self.summary_context_tokens - self.output_reserve_tokens

    def maximum_output_tokens(self, role):
        if role == "worker" and self.worker_max_output_tokens is not None:
            return self.worker_max_output_tokens
        if role == "summary" and self.summary_max_output_tokens is not None:
            return self.summary_max_output_tokens
        return self.main_max_output_tokens


def read_context_budget(environment: Mapping[str, str]) -> dict:
    if environment.get("A3_SUMMARY_INPUT_TOKENS"):
        raise ValueError("A3_SUMMARY_INPUT_TOKENS was removed; summaries use the full model window")
    main = int(environment.get("A3_MODEL_CONTEXT_TOKENS", "500000"))
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
            "output_reserve_tokens": "A3_OUTPUT_RESERVE_TOKENS",
            "main_max_output_tokens": "A3_MODEL_MAX_OUTPUT_TOKENS",
            "worker_max_output_tokens": "A3_WORKER_MAX_OUTPUT_TOKENS",
            "summary_max_output_tokens": "A3_SUMMARY_MAX_OUTPUT_TOKENS",
        }.items()
        if environment.get(key)
    }
    budget = ContextBudget(main_context_tokens=main, **windows, **options)
    return {name: value for name, value in asdict(budget).items() if value is not None}
