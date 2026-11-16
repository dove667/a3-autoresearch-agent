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
JOB_FAILED = "f" * 32
JOB_TRAIN = "9" * 32
PREPARED = "prepared-" + "e" * 32

FILES = {
    "bench/task.md": ("# 任务\n\n把 bench/measure.txt 里的入盒数报给我，并说明它来自哪次评测。\n"),
    "bench/measure.txt": (
        "run=2026-11-10-act-p142\ntask=2x10\nseeds=10010-10015\n"
        "cookies_in_target=7\nepisodes=120\nmax_score=20\n"
    ),
    "logs/train-f.log": (
        "step 4200/5000 loss 0.031\n"
        "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB "
        "(GPU 0; 23.65 GiB total capacity)\n"
    ),
    "experiments/2026-11-13-dagger-collect/report.json": (
        '{"target_saved": 400, "saved": 240, "attempts": 268, "batches": 3, '
        '"failures": {"wrong_mode": 12, "timeout": 16}}\n'
    ),
    "experiments/2026-11-10-act-p142/results/report.json": (
        '{"2x5": {"cookies_in_target": 29, "episodes": 60, "native_success": 4},'
        ' "2x10": {"cookies_in_target": 7, "episodes": 120, "native_success": 0}}\n'
    ),
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
    JOB_FAILED: {
        "status": "failed",
        "exit_code": 1,
        "backend": "process",
        "failure_class": "resource",
        "log_path": "logs/train-f.log",
    },
    JOB_TRAIN: {"status": "running", "exit_code": None, "backend": "process"},
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
    """List a real directory tree built from the fixture files."""
    prefix = (args.get("path") or "").strip("/")
    if prefix in (".", "./"):
        prefix = ""
    entries = {}
    for path in FILES:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :].lstrip("/")
        if not rest:
            continue
        head = rest.split("/")[0]
        if "/" in rest:
            entries.setdefault(head, {"name": head, "path": f"{prefix}/{head}".lstrip("/"),
                                      "type": "directory"})
        else:
            entries[head] = {"name": head, "path": path, "type": "file"}
    ordered = sorted(entries.values(), key=lambda entry: (entry["type"] != "directory", entry["name"]))
    return {"ok": True, "path": prefix, "entries": ordered[: args.get("limit", 50)]}


def search_files(args):
    """Search the fixture text, so a lookup can succeed or fail honestly."""
    query = str(args.get("query") or "")
    if not query:
        return {"ok": False, "error": "missing_arguments", "hint": "query is required."}
    matches = []
    for path, text in FILES.items():
        if not path.startswith((args.get("path") or "").strip("/")):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if query.lower() in line.lower():
                matches.append({"path": path, "line": number, "text": line[:200]})
                break
    return {"ok": True, "query": query, "matches": matches[:10]}


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


def job_list(args):
    return {
        "ok": True,
        "jobs": {
            job: {"status": row["status"], "exit_code": row.get("exit_code")}
            for job, row in JOBS.items()
        },
    }


ARTIFACTS = {
    JOB_FAILED: "logs/train-f.log",
    JOB_A: "experiments/2026-11-10-act-p142/results/report.json",
    JOB_GPU: "experiments/2026-11-10-act-p142/results/report.json",
}


def job_artifact(args):
    job_id = args.get("job_id")
    # The tool takes an explicit path; a fixture file is enough to serve it.
    requested = (args.get("path") or "").strip("/")
    path = ARTIFACTS.get(job_id)
    if requested in FILES and (path is None or requested.endswith(path.split("/")[-1])):
        path = requested
    if path is None:
        return {
            "ok": False,
            "error": "artifact_not_found",
            "hint": "This job has no stored artifact; check its status first.",
        }
    return {
        "ok": True,
        "job_id": job_id,
        "path": path,
        "content": _page(FILES[path]),
        "bytes": len(FILES[path]),
    }


def job_cancel(args):
    return {"ok": True, "job_id": args.get("job_id"), "status": "cancelled"}


def write_file(args):
    return {"ok": True, "path": args.get("path"), "bytes": len(str(args.get("content", "")))}


def export_list(args):
    return {"ok": True, "exports": [{"id": "export-p142", "path": "experiments/2026-11-10-act-p142"}]}


def export_status(args):
    return {"ok": True, "id": args.get("id"), "status": "ready"}


def runtime_status(args):
    return {
        "ok": True,
        "controller": "connected",
        "gpus": [{"index": 0, "free_mb": 22000}],
        "queue": {"running": 1, "queued": 0},
    }


def session_status(args):
    return {"ok": True, "status": "continue", "run": "2026-11-16_12-43-08"}


def session_list(args):
    return {"ok": True, "sessions": [{"id": "main", "role": "research", "is_main": True}]}


def research_status(args):
    return {"ok": True, "status": args.get("status", "continue"), "saved": True}


def idle_wait(args):
    return {"ok": True, "reason": "timeout", "observed": {}}


def evaluation_compare(args):
    return {
        "ok": True,
        "2x5": {"baseline": 14, "candidate": 29, "episodes": 60},
        "2x10": {"baseline": 11, "candidate": 7, "episodes": 120},
    }


def generic_ok(args):
    return {"ok": True, "note": "fixture has no effect for this tool"}


HANDLERS = {
    "read": read,
    "list": list_dir,
    "search_files": search_files,
    "job_submit": job_submit,
    "job_prepare": job_prepare,
    "job_status": job_status,
    "job_wait": job_wait,
    "job_list": job_list,
    "job_artifact": job_artifact,
    "job_cancel": job_cancel,
    "write": write_file,
    "edit": generic_ok,
    "export_list": export_list,
    "export_status": export_status,
    "export_create": generic_ok,
    "export_manifest": generic_ok,
    "export_delete": generic_ok,
    "export_share": generic_ok,
    "export_revoke": generic_ok,
    "runtime_status": runtime_status,
    "session_status": session_status,
    "session_list": session_list,
    "session_artifact": generic_ok,
    "session_stop": generic_ok,
    "session_cancel": generic_ok,
    "research_status": research_status,
    "idle_wait": idle_wait,
    "evaluation_compare": evaluation_compare,
    "conditions_compare": generic_ok,
    "vision_status": generic_ok,
    "vision_inspect": generic_ok,
    "web_status": lambda args: {"ok": True, "enabled": False},
    "web_search": lambda args: {"ok": False, "error": "web_disabled", "hint": "Disabled here."},
    "web_fetch": lambda args: {"ok": False, "error": "web_disabled", "hint": "Disabled here."},
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
