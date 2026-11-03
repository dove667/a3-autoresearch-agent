"""Optional operator-owned inference settings, shared by every model request."""

from collections.abc import Mapping
from copy import deepcopy


def validate_model_request_options(options: dict | None) -> dict:
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - {"reasoning", "service_tier"}:
        raise ValueError("Model options may contain only reasoning and Standard service tier")
    reasoning = options.get("reasoning")
    if reasoning is not None and (
        not isinstance(reasoning, dict)
        or set(reasoning) != {"effort"}
        or not isinstance(reasoning["effort"], str)
        or reasoning["effort"]
        not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    ):
        raise ValueError("Invalid operator reasoning effort")
    if "service_tier" in options and options["service_tier"] != "default":
        raise ValueError("Only explicit Standard service tier is supported by this setting")
    return deepcopy(options)


def read_model_request_options(environment: Mapping[str, str]) -> dict:
    options = {}
    if effort := environment.get("A3_MODEL_REASONING_EFFORT"):
        options["reasoning"] = {"effort": effort}
    if tier := environment.get("A3_MODEL_SERVICE_TIER"):
        options["service_tier"] = tier
    return validate_model_request_options(options)
