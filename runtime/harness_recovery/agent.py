import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import context_manager
import skill_tools
import web_tools

BASE = os.environ["A3_GATEWAY_URL"]
TOKEN = os.environ["A3_GATEWAY_TOKEN"]


def req(method, path, data=None):
    r = urllib.request.Request(
        BASE + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(r, timeout=300) as x:
            return json.loads(x.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:10000]}


def log(x):
    with open("trajectory.jsonl", "a") as f:
        f.write(json.dumps(x, ensure_ascii=False) + "\n")


def select_json_fields(raw, fields, path):
    if (
        not isinstance(fields, list)
        or not 1 <= len(fields) <= 32
        or any(not isinstance(k, str) or len(k) > 200 for k in fields)
    ):
        raise ValueError("fields must contain 1..32 bounded JSON object keys")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("field selection requires a JSON object")  # noqa: TRY004 - preserve tool contract
    return {
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fields": {k: value[k] for k in fields if k in value},
        "missing_fields": [k for k in fields if k not in value],
    }


def tool(name, a):
    if name in web_tools.ROUTES:
        return web_tools.dispatch(name, a)
    if name in skill_tools.ROUTES:
        return skill_tools.dispatch(name, a)
    if name == "gateway":
        path = a["path"]
        if not any(
            path == p or path.startswith((p + "/", p + "?"))
            for p in [
                "/jobs",
                "/sessions",
                "/versions",
                "/checks/comparison",
                "/fixtures/isolation",
                "/evolution",
            ]
        ):
            return "path not allowed"
        return req(a["method"], path, a.get("body"))
    if name in ("read", "write", "list"):
        p = pathlib.Path(a["path"]).resolve()
        roots = [
            pathlib.Path("/workspace"),
            pathlib.Path("/opt/a3"),
            pathlib.Path("/run/input"),
        ]
        if not any(p == r or r in p.parents for r in roots):
            return "denied"
        if name == "write":
            if not (p == roots[0] or roots[0] in p.parents):
                return "read only"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"])
            return "written"
        if name == "list":
            return [str(x) for x in p.iterdir()][:300]
        if a.get("fields") is not None:
            return select_json_fields(p.read_bytes(), a["fields"], str(p))
        s = p.read_text()
        return s[a.get("offset", 0) : a.get("offset", 0) + a.get("limit", 30000)]
    if name == "wait":
        time.sleep(min(a.get("seconds", 5), 30))
        return "waited"
    return "unknown tool"


def dispatch_tool_call(call):
    arguments = {}
    try:
        arguments = json.loads(call["arguments"])
        return tool(call["name"], arguments)
    except FileNotFoundError:
        return {"error": "file_not_found", "path": arguments.get("path")}
    except PermissionError:
        return {"error": "permission_denied", "path": arguments.get("path")}
    except Exception as error:  # noqa: BLE001 - keep private errors out of tool results
        return {"error": "tool_execution_failed", "error_type": type(error).__name__}


def spec(name, props, required):
    return {
        "type": "function",
        "name": name,
        "description": {
            "gateway": 'Call Host APIs. For remote job creation POST /jobs body {command:["python","-m","module","--help"],gpu:false,files:{}}; command MUST be an array. GET /jobs/{id} checks your own job. Other paths /sessions, /versions, /checks/comparison, /fixtures/isolation, /evolution. Jobs execute isolated code, not local host commands.',
            "read": "Read text file in workspace, public A3, or frozen inputs. For JSON metadata, optional fields selects only specified top-level keys and returns a whole-file SHA256 plus explicit missing_fields; without fields text offsets are unchanged.",
            "write": "Write text in workspace.",
            "list": "List accessible directory.",
            "wait": "Wait for asynchronous jobs.",
        }[name],
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "strict": False,
    }


S = {"type": "string"}
tools = (
    [
        spec(
            "gateway",
            {"method": S, "path": S, "body": {"type": "object"}},
            ["method", "path"],
        ),
        spec(
            "read",
            {
                "path": S,
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 32,
                },
            },
            ["path"],
        ),
        spec("write", {"path": S, "content": S}, ["path", "content"]),
        spec("list", {"path": S}, ["path"]),
        spec("wait", {"seconds": {"type": "integer"}}, []),
    ]
    + web_tools.TOOLS
    + skill_tools.TOOLS
)


def save_manifest():
    files = {
        "agent.py",
        "web_tools.py",
        "skill_tools.py",
        "context_manager.py",
        "task.txt",
        "checkpoint.json",
        "memory.jsonl",
        "resume.json",
        "continuation.json",
    }
    for manifest in ("resume.json", "continuation.json"):
        try:
            files.update(json.loads(pathlib.Path(manifest).read_text()).get("files", []))
        except (OSError, ValueError):
            pass
    for path in pathlib.Path(".").rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and not path.name.endswith(".tmp")
        ):
            files.add(path.as_posix())
    files = sorted(
        p for p in files if pathlib.Path(p).is_file() or p in ("resume.json", "continuation.json")
    )
    for filename, command in [
        ("resume.json", ["python", "agent.py", "--resume"]),
        ("continuation.json", ["python", "agent.py", "--resume"]),
    ]:
        context_manager.atomic(filename, json.dumps({"command": command, "files": files}))


def checkpoint(messages):
    save_manifest()
    context_manager.atomic("checkpoint.json", json.dumps(messages, ensure_ascii=False))


def checkpoint_hash():
    return hashlib.sha256(pathlib.Path("checkpoint.json").read_bytes()).hexdigest()


def recover_journal(messages):
    p = pathlib.Path("pending-calls.json")
    if not p.exists():
        context_manager.atomic(p, "null")
        return messages
    j = json.loads(p.read_text())
    if j is None:
        return messages
    if checkpoint_hash() != j["base_hash"]:
        ids = {m.get("call_id") for m in messages if m.get("type") == "function_call_output"}
        expected = {x["call_id"] for x in j["output"] if x.get("type") == "function_call"}
        if not expected.issubset(ids):
            raise RuntimeError("journal/checkpoint mismatch; manual recovery required")
        context_manager.atomic(p, "null")
        return messages
    restored = messages + j["output"]
    for c in (x for x in j["output"] if x.get("type") == "function_call"):
        value = j["results"].get(
            c["call_id"],
            {
                "error": "outcome_unknown_after_interruption",
                "instruction": "Do not replay blindly; inspect existing operation status.",
            },
        )
        restored.append(
            {
                "type": "function_call_output",
                "call_id": c["call_id"],
                "output": json.dumps(value, ensure_ascii=False),
            }
        )
    checkpoint(restored)
    context_manager.atomic(p, "null")
    save_manifest()
    return restored


if os.path.exists("checkpoint.json") and "--resume" in sys.argv:
    with open("checkpoint.json", encoding="utf-8") as stream:
        messages = json.load(stream)
else:
    with open("task.txt", encoding="utf-8") as stream:
        messages = [{"role": "user", "content": stream.read()}]
try:
    if pathlib.Path("checkpoint.json").exists():
        messages = recover_journal(messages)
except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
    log({"recovery_error": str(e)})
    sys.exit(1)
empty = 0
while True:
    try:
        try:
            context_manager.configure(req)
            prepared, record = context_manager.prepare(messages, tools, req)
        except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
            log(
                {
                    "context_error": str(e),
                    "state": "original checkpoint preserved; recovery required",
                }
            )
            context_manager.atomic(
                "context-failure.json",
                json.dumps({"error": str(e), "state": "uncompacted checkpoint preserved"}),
            )
            save_manifest()
            break
        if record:
            checkpoint(prepared)
            messages = prepared
            log({"context_compaction": record})
        checkpoint(messages)
        request = {
            "model": "openai/sandbox-model",
            "instructions": "You are an autonomous A3 research agent. Use tools, preserve evidence and checkpoints, follow frozen inputs and host protocol. Never reveal credentials.",
            "input": messages,
            "tools": tools,
        }
        log({"model_request_tools": tools})
        resp = req("POST", "/v1/responses", request)
        log({"response": resp})
        out = resp.get("output", [])
        if not out:
            empty += 1
            log({"error": "empty response", "data": resp, "attempt": empty})
            if empty >= 3:
                checkpoint(messages)
                break
            time.sleep(10)
            continue
        empty = 0
        calls = [x for x in out if x.get("type") == "function_call"]
        journal = {"base_hash": checkpoint_hash(), "output": out, "results": {}}
        context_manager.atomic("pending-calls.json", json.dumps(journal, ensure_ascii=False))
        save_manifest()
        messages.extend(out)
        for c in calls:
            result = dispatch_tool_call(c)
            journal["results"][c["call_id"]] = result
            context_manager.atomic("pending-calls.json", json.dumps(journal, ensure_ascii=False))
            log(
                {
                    "tool": c["name"],
                    "call_id": c["call_id"],
                    "arguments": c["arguments"],
                    "result": result,
                }
            )
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": c["call_id"],
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )
        checkpoint(messages)
        context_manager.atomic("pending-calls.json", "null")
        save_manifest()
        if "--test" in sys.argv and calls:
            break
        if not calls:
            if pathlib.Path("run-outcome.json").exists() and json.loads(
                pathlib.Path("run-outcome.json").read_text()
            ).get("status") in ("complete", "blocked"):
                break
            messages.append(
                {
                    "role": "user",
                    "content": "Continue autonomously; do not stop until all acceptance passes or a genuine external blocker is documented. Check actual Host main identity before any handoff. Save evidence and state.",
                }
            )
    except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
        log({"loop_error": type(e).__name__, "state": "checkpoint and journal retained"})
        break
