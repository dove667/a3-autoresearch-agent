"""Answer tool calls from fixed fixtures so a model can be scored without a host.

The sandbox keeps the real contract: schemas come from `tool_catalog`, arguments
are checked the way the Harness checks them, and a wrong argument returns the same
kind of structured error a real run returns.
"""

import json
import re

from a3_research_agent.harness import tool_catalog

JOB_A = "a" * 32
JOB_B = "b" * 32
JOB_CANCELLED = "c" * 32
JOB_GPU = "d" * 32
PREPARED = "prepared-" + "e" * 32

FILES = {
    "bench/task.md": ("# 任务\n\n把 bench/measure.txt 里的入盒数报给我，并说明它来自哪次评测。\n"),
    "bench/measure.txt": "run=2026-11-10-act-p142\nseed=10010\ncookies_in_target=7\nmax_score=20\n",
    "experiments/2026-11-10-act-p142/config.json": (
        '{"seeds": {"2x5": [10000, 10001, 10002, 10003, 10004, 10005],'
        ' "2x10": [10010, 10011, 10012, 10013, 10014, 10015]},'
        ' "checkpoint_sha256": "2672a2764fcf22fd1be638f40af657990aba773270040781ed9895750199a7f6"}\n'
    ),
}

JOBS = {
    JOB_A: {"status": "completed", "exit_code": 0, "backend": "process"},
    JOB_GPU: {"status": "completed", "exit_code": 0, "backend": "process"},
    JOB_CANCELLED: {"status": "cancelled", "exit_code": None, "backend": "process"},
}

PLAN = {
    "revision": 202,
    "direction": {"objective": "提高 A3 2×5 与 2×10 的入盒准确率与通过率"},
    "routes": {"act-p142": {"status": "active", "question": "扩采 DAgger 后继续训练 ACT"}},
    "steps": {
        "s5-closedloop-compare": {
            "status": "pending",
            "route": "act-p142",
            "task": "同种子下 2×10 及 2×5 保持性配对比较新旧 checkpoint",
        },
        "s4-act-train": {"status": "done", "route": "act-p142", "task": "温启动 ACT 5000 步"},
    },
}

MEMORY = {
    "38fa2b462603": "2×5 配对：5k 入盒 14/60，20k 29/60；20k seed10000 原生 success=True。",
    "d9ebbacf33e6": "2×10 配对：5k 入盒 11/120，20k 7/120；两版原生 success 均 0/6。",
}

SKILLS = {
    "a3-mujoco-autoresearch": "A3 机器人策略实验：选择 ACT、扩散/流、VLA 方法，诊断数据与训练，做配对闭环评测。",
    "a3-job-recovery": "提交和恢复 A3 远程作业：源码附件、CPU 预检、批量提交、提交未知、排队与失败重试。",
}


def schema_of(name):
    return next((tool["parameters"] for tool in tool_catalog.TOOLS if tool["name"] == name), None)


def check_arguments(name, arguments):
    """Reject an argument object the real contract would reject."""
    schema = schema_of(name)
    if schema is None:
        return "unknown_tool", "Choose one of the advertised tools."
    if not isinstance(arguments, dict):
        return "invalid_arguments", "Arguments must be a JSON object."
    if schema.get("additionalProperties") is False:
        extra = arguments.keys() - set(schema.get("properties", {}))
        if extra:
            return "invalid_arguments", f"Unknown field: {min(extra)}"
    for field in schema.get("required", []):
        if field not in arguments or arguments[field] in (None, ""):
            return "missing_arguments", f"{field} is required."
    for field, rule in schema.get("properties", {}).items():
        if field not in arguments:
            continue
        value = arguments[field]
        kinds = {
            "string": str,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected = kinds.get(rule.get("type", ""))
        if expected and not isinstance(value, expected):
            return "invalid_arguments", f"{field} must be {rule['type']}."
        if "enum" in rule and value not in rule["enum"]:
            return "invalid_arguments", f"{field} must be one of {rule['enum']}."
    return None, None


def _page(text, offset=0, limit=4000):
    return text[offset : offset + limit]


def read(args):
    path = args["path"]
    if path not in FILES:
        return {
            "ok": False,
            "error": "file_not_found",
            "hint": "Read requires an existing file; use list to select one.",
        }
    text = FILES[path]
    return {
        "ok": True,
        "path": path,
        "content": _page(text),
        "bytes": len(text),
        "next_offset": None,
    }


def list_dir(args):
    prefix = (args.get("path") or "").strip("/")
    names = sorted(path for path in FILES if path.startswith(prefix)) if prefix else sorted(FILES)
    if not names:
        return {"ok": True, "path": prefix, "entries": []}
    entries = [
        {"name": path.split("/")[-1], "path": path, "type": "file"}
        for path in names[: args.get("limit", 50)]
    ]
    return {"ok": True, "path": prefix, "entries": entries}


def job_submit(args):
    """Mirror the real contract: a GPU request is refused unless it was prepared."""
    if "prepared_id" in args and args["prepared_id"] != PREPARED:
        return {"ok": False, "error": "prepared_id_unknown", "hint": "Prepare the request first."}
    if "prepared_id" not in args:
        entries = args.get("jobs") or [args]
        gpu = any(isinstance(item, dict) and item.get("gpu") for item in entries)
        if gpu:
            return {
                "ok": False,
                "error": "gpu_job_needs_preparation",
                "hint": "Call job_prepare with this GPU request, then job_submit(prepared_id).",
            }
    if isinstance(args.get("jobs"), list):
        return {"ok": True, "jobs": [{"ok": True, "job_id": JOB_A, "status": "queued"}]}
    return {"ok": True, "job_id": JOB_A, "status": "queued"}


def job_prepare(args):
    requests = args.get("jobs") if isinstance(args.get("jobs"), list) else [args]
    return {
        "ok": True,
        "prepared": [
            {"ok": True, "prepared_id": PREPARED, "preflight_job_id": JOB_B} for _ in requests
        ],
    }


def job_status(args):
    job_id = args["job_id"]
    if job_id not in JOBS:
        return {
            "ok": False,
            "error": "invalid_arguments",
            "hint": "job_id must be a 32-character job identifier from a receipt.",
        }
    return {"ok": True, "job_id": job_id, **JOBS[job_id]}


def job_wait(args):
    return {
        "ok": True,
        "reason": "changed",
        "observed": {JOB_A: {"status": "completed", "exit_code": 0}},
    }


def plan_tool(args):
    action = args.get("action", "show")
    if action == "show":
        return {"ok": True, **PLAN}
    return {"ok": True, "ok_revision": True, "revision": PLAN["revision"] + 1}


def memory_tool(args):
    action = args.get("action", "read")
    if action in ("search", "list"):
        return {
            "ok": True,
            "items": [
                {"id": key, "kind": "result", "preview": text[:160], "score": 0.8}
                for key, text in MEMORY.items()
            ],
        }
    entry = MEMORY.get(args.get("id", ""))
    if entry is None:
        return {"ok": False, "error": "memory_not_found", "hint": "Search before reading."}
    return {"ok": True, "id": args["id"], "content": entry, "status": "active"}


def skills_tool(args):
    action = args.get("action", "list")
    if action == "list":
        return {
            "ok": True,
            "skills": [
                {"id": key, "description": text, "match_reason": "query"}
                for key, text in SKILLS.items()
            ],
        }
    if args.get("id") in SKILLS:
        return {"ok": True, "id": args["id"], "content": SKILLS[args["id"]]}
    return {"ok": False, "error": "skill_not_found", "hint": "List skills first."}


def recall(args):
    return {
        "ok": True,
        "results": [
            {
                "path": "conversation/context-history/call-7.txt",
                "excerpt": "ModuleNotFoundError: No module named 'lerobot' 出现在训练作业日志尾部",
            }
        ],
    }


HANDLERS = {
    "read": read,
    "list": list_dir,
    "search_files": lambda args: {"ok": True, "matches": [], "note": "no match in fixtures"},
    "job_submit": job_submit,
    "job_prepare": job_prepare,
    "job_status": job_status,
    "job_wait": job_wait,
    "gpu_status": lambda args: {"ok": True, "gpus": [{"index": 0, "free_mb": 22000}]},
    "plan": plan_tool,
    "memory": memory_tool,
    "skills": skills_tool,
    "recall": recall,
}


def execute(call, invalid):
    """Run one model tool call against the fixtures and record contract violations."""
    name = call.get("name") or ""
    try:
        args = json.loads(call.get("arguments") or "{}")
    except ValueError:
        invalid.append((name, "arguments are not valid JSON"))
        return {
            "ok": False,
            "error": "invalid_tool_json",
            "hint": "Send arguments as a JSON object.",
        }
    problem, hint = check_arguments(name, args)
    if problem:
        invalid.append((name, hint))
        return {"ok": False, "error": problem, "hint": hint}
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "ok": False,
            "error": "unknown_tool",
            "hint": "Choose a tool from the advertised list.",
        }
    return handler(args)


def job_id_of(text):
    return re.fullmatch(r"[0-9a-f]{32}", text or "") is not None
