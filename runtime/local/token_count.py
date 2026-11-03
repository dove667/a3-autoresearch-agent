"""Offline o200k counts for explicit estimates, not provider-exact billing."""

import json
from functools import lru_cache


@lru_cache(maxsize=1)
def encoding():
    from litellm.rust_bridge.tokenizer import OpenAIEncoding

    return OpenAIEncoding.from_tiktoken("o200k_base")


def count_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(encoding().encode_ordinary(text))


def estimate_request_tokens(arguments: dict, provider_reserve: int = 4096) -> int:
    messages = arguments.get("input", [])
    return (
        count_tokens(arguments)
        + provider_reserve
        + 16
        + 16 * (len(messages) if isinstance(messages, list) else 1)
        + 16 * len(arguments.get("tools") or [])
    )
