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


def validate_role_models(routes: dict | None) -> dict:
    if routes is None:
        return {}
    if not isinstance(routes, dict) or set(routes) - {"worker", "summary"}:
        raise ValueError("Only worker and summary model overrides are configurable")
    result = {}
    for role, route in routes.items():
        if not isinstance(route, dict) or set(route) != {"model_name", "request_options"}:
            raise ValueError("Role models require a name and request options")
        name = route["model_name"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 256
            or any(c.isspace() for c in name)
            or "://" in name
        ):
            raise ValueError("Invalid operator model identifier")
        result[role] = {
            "model_name": name,
            "request_options": validate_model_request_options(route["request_options"]),
        }
    return result


def read_role_models(environment: Mapping[str, str]) -> dict:
    routes = {}
    for role in ("worker", "summary"):
        prefix = "A3_" + role.upper()
        name = environment.get(prefix + "_MODEL_NAME")
        effort = environment.get(prefix + "_REASONING_EFFORT")
        if effort and not name:
            raise ValueError("A role reasoning setting requires its model name")
        if name:
            options = {"service_tier": "default"}
            if effort:
                options["reasoning"] = {"effort": effort}
            routes[role] = {"model_name": name, "request_options": options}
    return validate_role_models(routes)
