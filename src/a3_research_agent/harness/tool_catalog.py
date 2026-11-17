"""Declare the complete high-level interface shown to the research model."""

from . import control_tools, file_tools, host_tools, job_tools, research_web, skill_registry

KNOWLEDGE_TOOLS = [
    {
        "type": "function",
        "name": "plan",
        "description": "Keep the shared research direction, routes and execution steps. show reads the "
        "current revision; update patches changed fields using that revision. Every new "
        "route needs question, hypothesis, experiment and decision_rule. Steps link to a "
        "route and need task and criterion. After results, review the route with "
        "conclusion, evidence and decision continue/pause/complete; continuing also needs "
        "measured metrics, and step optionally names the step the decision closes. All "
        "tasks and job progress belong here.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "update", "review"]},
                "revision": {"type": "integer", "minimum": 0},
                "reason": {"type": "string"},
                "id": {"type": "string"},
                "direction": {
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string"},
                        "metrics": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    },
                    "additionalProperties": False,
                },
                "routes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "question": {"type": "string"},
                            "hypothesis": {"type": "string"},
                            "experiment": {"type": "string"},
                            "decision_rule": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["proposed", "active", "paused", "completed"],
                            },
                            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                            "conclusion": {"type": "string"},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 20,
                            },
                        },
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                },
                "decision": {"type": "string", "enum": ["continue", "pause", "complete"]},
                "conclusion": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "step": {"type": "string"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 160},
                    "maxItems": 8,
                },
                "archive": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "steps": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "route": {"type": "string"},
                            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                            "task": {"type": "string"},
                            "criterion": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "active", "blocked", "done", "skipped"],
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 8,
                            },
                            "jobs": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                            "progress": {"type": "string"},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 4,
                            },
                        },
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "memory",
        "description": "Store learned research experience across runs. search(query) returns summaries; "
        "read(id) expands one note. write(content,scope,refs,kind?) saves a finding. "
        "update(id,revision,reason,content?) corrects it; "
        "archive/restore(id,revision,reason) changes visibility. Read first for revision. "
        "sources(id) lists evidence; sources(id,ref) reads it. Similar writes suggest an "
        "existing ID; force=true with reason confirms a distinct finding. lifetime=run "
        "limits applicability to run_ids. Use plan for future work and skills for "
        "procedures.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "write",
                        "read",
                        "list",
                        "search",
                        "update",
                        "archive",
                        "sources",
                        "restore",
                    ],
                },
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "scope": {"type": "string"},
                "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
                "supersedes": {"type": "string"},
                "id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "reason": {"type": "string"},
                "lifetime": {"type": "string", "enum": ["durable", "run"]},
                "run_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
                "include_archived": {"type": "boolean"},
                "force": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
                "ref": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "Natural-language research question for search.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "recall",
        "description": "Search original messages and tool results from the CURRENT run only. keywords is "
        "a list of terms. Omit recent_rounds for all current-run history; set it to "
        "inspect only the latest N decision rounds. Default evidence mode avoids "
        "retrieval echoes; raw includes tool arguments. Both modes exclude prior recall results. "
        "Search follows hash-verified archived tool outputs and UTF-8 artifacts, returning "
        "bounded snippets with source path, hash and character offset for read. Use memory for cross-run "
        "findings and memory sources for referenced older evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 12,
                },
                "recent_rounds": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                "mode": {"type": "string", "enum": ["evidence", "raw"]},
            },
            "required": ["keywords"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]


TOOL_FAMILIES = (
    ("files", "文件与工作区", file_tools.TOOLS),
    ("jobs", "远程作业", job_tools.TOOLS),
    ("runtime", "运行与评估", host_tools.TOOLS),
    ("control", "等待与状态", control_tools.TOOLS),
    ("knowledge", "计划与记忆", KNOWLEDGE_TOOLS),
    ("web", "网络", research_web.TOOLS),
    ("skills", "技能", skill_registry.TOOLS),
)
TOOLS = tuple(entry for _, _, entries in TOOL_FAMILIES for entry in entries)
BY_NAME = {entry["name"]: entry for entry in TOOLS}
OPTIONAL = {
    "web": frozenset({"web_search", "web_fetch", "web_status"}),
}


def enabled(capabilities):
    """Hide optional tool families the current deployment cannot serve.

    A weak model should never spend a decision on a tool that can only return
    "not configured"; dispatch still accepts every registered name.
    """
    if not isinstance(capabilities, dict):
        return list(TOOLS)
    hidden = set()
    for family, names in OPTIONAL.items():
        if capabilities.get(family) is False:
            hidden |= names
    return [entry for entry in TOOLS if entry["name"] not in hidden]
