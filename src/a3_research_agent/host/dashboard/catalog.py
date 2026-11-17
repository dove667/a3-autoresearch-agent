"""Read the model-visible high-level tool catalog for human review."""

from a3_research_agent.harness import tool_catalog

TYPE_LABELS = {
    "string": "文本",
    "integer": "整数",
    "number": "数值",
    "boolean": "布尔",
    "array": "列表",
    "object": "对象",
}


def _type_label(schema):
    if not isinstance(schema, dict):
        return "任意"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " 或 ".join(TYPE_LABELS.get(value, str(value)) for value in kind)
    if kind == "array":
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(items.get("type"), str):
            inner = TYPE_LABELS.get(items["type"], items["type"])
            return f"列表[{inner}]"
        return "列表"
    if kind == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            return f"对象（{len(properties)} 个字段）"
        return "对象"
    if isinstance(kind, str) and kind:
        return TYPE_LABELS.get(kind, kind)
    return "任意"


def _choices(schema):
    values = schema.get("enum") if isinstance(schema, dict) else None
    if not isinstance(values, list):
        return []
    return [str(value) for value in values[:12]]


def _parameters(schema):
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    parameters = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        parameters.append(
            {
                "name": str(name),
                "type": _type_label(spec),
                "required": name in required,
                "description": spec.get("description") or "",
                "choices": _choices(spec),
            }
        )
    return parameters


def load_tools():
    """Return the complete catalog with stable families and parameter summaries."""
    optional_names = (
        set().union(*tool_catalog.OPTIONAL.values()) if tool_catalog.OPTIONAL else set()
    )
    items = []
    families = []
    for family_id, label, entries in tool_catalog.TOOL_FAMILIES:
        family_items = []
        for entry in entries:
            parameters = _parameters(entry.get("parameters"))
            item = {
                "name": entry["name"],
                "description": entry.get("description") or "",
                "family": family_id,
                "family_label": label,
                "optional": entry["name"] in optional_names,
                "parameters": parameters,
            }
            family_items.append(item)
            items.append(item)
        families.append({"id": family_id, "label": label, "count": len(family_items)})
    return {
        "items": items,
        "families": families,
        "counts": {
            "total": len(items),
            "families": len(families),
            "optional": sum(item["optional"] for item in items),
        },
    }
