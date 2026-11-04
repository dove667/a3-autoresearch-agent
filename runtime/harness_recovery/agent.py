import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import analyzer_client
import compaction
import context_manager
import memory_state
import recall as recall_tools

# The derived harness ships without the generated extension modules. Their
# absence must not stop the loop: lookups below tolerate empty ROUTES/TOOLS.
try:
    import skill_tools
except ImportError:
    skill_tools = None
try:
    import web_tools
except ImportError:
    web_tools = None
import work_assignments

BASE = os.environ["A3_GATEWAY_URL"]
TOKEN = os.environ["A3_GATEWAY_TOKEN"]


def req(method, path, data=None, *, timeout=300):
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
        with urllib.request.urlopen(r, timeout=timeout) as x:
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
    result = {
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fields": {k: value[k] for k in fields if k in value},
        "missing_fields": [k for k in fields if k not in value],
    }
    if context_manager.size(result) > 10000:
        return {
            "path": path,
            "sha256": result["sha256"],
            "error": "field_selection_exceeds_page_budget",
            "notice": "Selected values were not delivered. Use bounded character pages with offset/limit and source hash.",
        }
    return result


def tool(name, a):
    if name == "recall":
        return recall_tools.recall_tool(a)
    if name == "memory":
        return recall_tools.memory_tool(a)
    if name in getattr(web_tools, "ROUTES", {}):
        return web_tools.dispatch(name, a)
    if name in getattr(skill_tools, "ROUTES", {}):
        return skill_tools.dispatch(name, a)
    if name == "delegate":
        return work_assignments.dispatch(req, a)
    if name == "progress":
        return memory_state.progress(a)
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
                "/telemetry",
                "/analysis",
            ]
        ):
            return "path not allowed"
        work_assignments.guard_gateway(req, a["method"], path, a.get("body"))
        if a["method"] == "POST" and path == "/evolution/trials":
            analyzer.trigger("before_handoff")
        result = req(a["method"], path, a.get("body"))
        try:
            memory_state.record_gateway(a["method"], path, result)
        except Exception:  # noqa: BLE001 — preserve the result of an already executed operation
            try:
                log({"operational_receipt": "gateway_update_failed"})
            except OSError:
                pass  # Optional diagnostics cannot hide an executed operation.
        return result
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
            try:
                memory_state.record_write(p)
            except Exception:  # noqa: BLE001 — the file write already succeeded
                try:
                    log({"operational_receipt": "write_update_failed"})
                except OSError:
                    pass  # Optional diagnostics cannot hide an executed operation.
            return "written"
        if name == "list":
            return [str(x) for x in p.iterdir()][:300]
        if a.get("fields") is not None:
            if p.stat().st_size > 1024 * 1024:
                return {
                    "error": "field_selection_source_too_large",
                    "path": str(p),
                    "notice": "Use bounded offset/limit text pages; no JSON fields were read.",
                }
            return select_json_fields(p.read_bytes(), a["fields"], str(p))
        return memory_state.read_page(p, a.get("offset", 0), a.get("limit", 4000))
    if name == "wait":
        return work_assignments.wait_for_change(
            req,
            a.get("seconds", 5),
            cursor=WAIT_CURSOR,
            signals=lambda timeout: req("GET", "/wait/signals", timeout=timeout),
        )
    return "unknown tool"


# Only exact, locally defined validation messages may become public advice.
# Unexpected exceptions retain the generic boundary; never echo their text.
SAFE_VALIDATION_ERRORS = {
    "progress": {
        "Invalid bounded operational item": (
            "progress_bounds",
            "Use id 1..80 characters, status open|waiting|closed and note at most 400 characters.",
        ),
        "Evidence must be bounded workspace-relative references": (
            "progress_evidence",
            "Use at most 8 workspace-relative evidence paths, each at most 300 characters; no URLs, absolute paths or traversal.",
        ),
        "Closed operational items require evidence references, not a pass label": (
            "progress_evidence_required",
            "Closing an item requires actual evidence references; do not substitute a success label.",
        ),
        "Operational item capacity reached; preserve and consolidate explicitly": (
            "progress_capacity",
            "Operational item capacity reached. Inspect existing items; preserve history and consolidate explicitly rather than dropping records.",
        ),
    },
    "delegate": {
        "Assignment needs a bounded id and policy|harness role": (
            "assignment_identity",
            "Use an id matching [A-Za-z0-9][A-Za-z0-9_-]{0,63} and role policy|harness.",
        ),
        "Assignment needs a bounded objective and stop condition": (
            "assignment_objective",
            "assign requires objective 1..8000 characters and stop_condition 1..4000 characters.",
        ),
        "Assignment needs 1..16 relative deliverables": (
            "assignment_deliverables",
            "assign requires 1..16 workspace-relative deliverable paths.",
        ),
        "Assignment context or export count exceeds bound": (
            "assignment_context_count",
            "Use at most 32 context_files and 64 read_only_exports.",
        ),
        "Select research context only; never copy checkpoint or private logs": (
            "assignment_context_scope",
            "Context files must be inside research/ or orchestration/results/. Use permitted sourced research notes; never copy private history or bypass scope.",
        ),
        "Private/control files cannot be assignment context": (
            "assignment_private_context",
            "Private/control names cannot be context. Do not rename secrets to bypass this restriction.",
        ),
        "Worker package exceeds existing Host upload bound": (
            "assignment_package_size",
            "The complete worker package exceeds the existing upload bound. Reduce the selected inputs or use existing permitted sealed exports; do not omit required evidence or broaden access.",
        ),
    },
    "wait": {
        "Wait seconds must be an integer in 0..600": (
            "wait_bounds",
            "Use an integer seconds value in 0..600.",
        )
    },
}


def dispatch_tool_call(call):
    arguments = {}
    try:
        arguments = json.loads(call["arguments"])
        return tool(call["name"], arguments)
    except FileNotFoundError:
        return {"error": "file_not_found", "path": arguments.get("path")}
    except PermissionError:
        return {"error": "permission_denied", "path": arguments.get("path")}
    except ValueError as error:
        advice = SAFE_VALIDATION_ERRORS.get(call.get("name"), {}).get(str(error))
        if advice:
            return {"error": "validation_failed", "code": advice[0], "hint": advice[1]}
        return {"error": "tool_execution_failed", "error_type": type(error).__name__}
    except Exception as error:  # noqa: BLE001 - keep private errors out of tool results
        return {"error": "tool_execution_failed", "error_type": type(error).__name__}


class ToolResultPersistenceError(RuntimeError):
    def __init__(self, phase):
        super().__init__(
            "Executed tool result could not be published; inspect journal, never replay blindly"
        )
        self.phase = phase


def save_tool_result(call, result, journal, messages):
    """Persist the executed outcome before optional receipts or presentation work."""
    phase = "raw_result_journal"
    try:
        journal["results"][call["call_id"]] = result
        context_manager.atomic("pending-calls.json", json.dumps(journal, ensure_ascii=False))
        phase = "result_receipt"
        if call["name"] == "read":
            memory_state.record_page(call["call_id"], result)
        phase = "result_archival"
        bounded = context_manager.bound_tool_result(result, call)
        journal["results"][call["call_id"]] = bounded
        context_manager.atomic("pending-calls.json", json.dumps(journal, ensure_ascii=False))
        log(
            {
                "tool": call["name"],
                "call_id": call["call_id"],
                "arguments": call.get("arguments", "{}"),
                "result": bounded,
            }
        )
        value = context_manager.output_message(bounded, call["call_id"])
        messages.append(value)
        recall_tools.append_history(value)
    except Exception as error:
        raise ToolResultPersistenceError(phase) from error


def spec(name, props, required):
    return {
        "type": "function",
        "name": name,
        "description": {
            "gateway": 'Call Host APIs. For remote job creation POST /jobs body {command:["python","-m","module","--help"],gpu:false,files:{}}; command MUST be an array. GET /jobs/{id} checks your own job. Other paths /sessions, /versions, /checks/comparison, /fixtures/isolation, /evolution. Jobs execute isolated code, not local host commands.',
            "read": "Read a bounded character page with content, whole-file SHA256, offset/end/total and EOF. limit is 1..8000, default 4000. Same-path changed hashes are different sources. Optional fields selects top-level JSON keys instead.",
            "write": "Write text in workspace.",
            "list": "List accessible directory.",
            "delegate": "Coordinate bounded policy/harness worker assignments: assign, collect verified reports, decide, or list (bounded summary; list with id returns the complete record). Workers use fresh contexts and existing identities; no new permission or automatic scientific adoption.",
            "progress": "List or explicitly update bounded operational items with evidence references. These are your claims, not validated scientific acceptance; closed items remain recorded across summaries.",
            "wait": "Wait up to 600 seconds for existing worker/job state changes, input changes or deadline; observation errors return immediately. No work is submitted or collected.",
            "recall": (
                "Search original history by keyword over an n..m round window "
                "(n=1 is the most recent round). Use after compaction or whenever "
                "earlier evidence is missing. Returns labelled snippets from the "
                "original messages; never reconstruct missing details from memory."
            ),
            "memory": (
                "Append a durable note to memory.jsonl (action=write) or read/list "
                "entries (action=read|list). Notes survive compaction and new sessions."
            ),
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
        spec(
            "progress",
            {
                "action": {"type": "string", "enum": ["list", "update"]},
                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                "status": {"type": "string", "enum": ["open", "waiting", "closed"]},
                "note": {"type": "string", "maxLength": 400},
                "evidence": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                },
            },
            ["action"],
        ),
        spec(
            "delegate",
            {
                "action": {
                    "type": "string",
                    "enum": ["assign", "collect", "decide", "list", "reconcile"],
                },
                "id": S,
                "role": {"type": "string", "enum": ["policy", "harness"]},
                "objective": {"type": "string", "minLength": 1, "maxLength": 8000},
                "stop_condition": {"type": "string", "minLength": 1, "maxLength": 4000},
                "deliverables": {"type": "array", "minItems": 1, "maxItems": 16, "items": S},
                "context_files": {"type": "array", "maxItems": 32, "items": S},
                "read_only_exports": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "pattern": "^export-[a-f0-9]{32}$"},
                },
                "decision": {"type": "string", "enum": ["adopt", "reject", "follow_up"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            ["action"],
        ),
        spec("write", {"path": S, "content": S}, ["path", "content"]),
        spec("list", {"path": S}, ["path"]),
        spec("wait", {"seconds": {"type": "integer", "minimum": 0, "maximum": 600}}, []),
        spec(
            "recall",
            {
                "keywords": {"type": "array", "items": S},
                "n": {"type": "integer"},
                "m": {"type": "integer"},
                "window": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            ["keywords", "n", "m"],
        ),
        spec(
            "memory",
            {"action": S, "content": S, "key": S, "kind": S},
            ["action"],
        ),
    ]
    + list(getattr(web_tools, "TOOLS", []))
    + list(getattr(skill_tools, "TOOLS", []))
)


WORKER_TASK = work_assignments.worker_task(sys.argv)
INSTRUCTIONS = work_assignments.decision_instructions(WORKER_TASK)
compactor = compaction.ForkCompactor(req, tools=list(tools), instructions=INSTRUCTIONS)


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
    manifests = {}
    for manifest in ("resume.json", "continuation.json"):
        try:
            data = json.loads(pathlib.Path(manifest).read_text())
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise TypeError("Existing handoff manifest must be an object")
        manifests[manifest] = data
        files.update(data.get("files", []))
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
        ("resume.json", work_assignments.entry_command(WORKER_TASK)),
        ("continuation.json", work_assignments.entry_command(WORKER_TASK)),
    ]:
        data = {**manifests[filename], "command": command, "files": files}
        context_manager.atomic(filename, json.dumps(data))


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
        if c.get("name") == "read":
            memory_state.record_page(c["call_id"], value)
        restored.append(
            {
                "type": "function_call_output",
                "call_id": c["call_id"],
                "output": json.dumps(
                    context_manager.bound_tool_result(value, c), ensure_ascii=False
                ),
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
    if not pathlib.Path(recall_tools.HISTORY_PATH).exists():
        recall_tools.append_history_many(messages)
try:
    if pathlib.Path("checkpoint.json").exists():
        messages = recover_journal(messages)
    context_manager.restore_artifact_views(messages)
    recall_tools.ensure_history(messages)
    work_assignments.install_input_skills()
except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
    log({"recovery_error": str(e)})
    sys.exit(1)
WAIT_CURSOR = work_assignments.wait_cursor(messages)
analyzer = analyzer_client.start(req, memory_state.signals)
empty = 0
loop_phase = "decision_prepare"
while True:
    try:
        loop_phase = "decision_prepare"
        if work_assignments.should_retire(req, WORKER_TASK):
            checkpoint(messages)
            log({"role_transition": "former_main_saved_and_exiting"})
            break
        try:
            context_manager.configure(req)
        except Exception as e:  # noqa: BLE001 - preserve the checkpoint when Host budgets are unknown
            log({"context_config_error": type(e).__name__, "state": "checkpoint preserved"})
            context_manager.atomic(
                "context-failure.json",
                json.dumps(
                    {
                        "error": "context_config_unavailable",
                        "state": "original checkpoint preserved",
                    }
                ),
            )
            save_manifest()
            break
        operational_data = memory_state.view()
        operational_data["independent_analysis"] = analyzer.view()
        operational_data["assignments"] = work_assignments.view()
        if WORKER_TASK is not None:
            operational_data["worker_delivery"] = work_assignments.worker_delivery_status(
                WORKER_TASK
            )
        operational = {
            "role": "user",
            "content": json.dumps(operational_data, ensure_ascii=False),
        }
        state_reference = context_manager.archive(operational, "operational-view")
        # Asynchronous fork: this decision keeps the full history; a finished
        # compaction result is applied before a later decision, never mid-flight.
        # The operational view rides outside history, so only its size is budgeted.
        messages, compaction_record = compactor.before_request(
            messages, extra_messages=[operational]
        )
        if compaction_record:
            log({"context_compaction": compaction_record})
            if compaction_record.get("kind") == "applied":
                checkpoint(messages)
        checkpoint(messages)
        request = {
            "model": "openai/sandbox-model",
            "instructions": INSTRUCTIONS,
            "input": messages + [operational],
            "tools": tools,
        }
        log({"model_request_tools": tools})
        try:
            req("POST", "/telemetry/observations", memory_state.counters(), timeout=2)
        except Exception:  # noqa: BLE001 — optional telemetry never blocks research
            log({"optional_telemetry": "unavailable"})
        log({"operational_view": state_reference})
        loop_phase = "model_request"
        resp = req("POST", "/v1/responses", request)
        if resp.get("status") == "completed":
            memory_state.mark_delivered(messages)
            memory_state.decision_completed()
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
        # All waits in this response use the pre-decision cursor: a tool result
        # is not yet a normal model decision observing the notification.
        WAIT_CURSOR = work_assignments.wait_cursor(messages)
        messages.extend(out)
        recall_tools.append_history_many(out)
        for c in calls:
            loop_phase = "tool_execution"
            result = dispatch_tool_call(c)
            loop_phase = "result_persistence"
            save_tool_result(c, result, journal, messages)
        loop_phase = "checkpoint_publication"
        checkpoint(messages)
        context_manager.atomic("pending-calls.json", "null")
        save_manifest()
        if work_assignments.worker_result_ready(WORKER_TASK):
            break
        if "--test" in sys.argv and calls:
            break
        if not calls:
            if (
                WORKER_TASK is None
                and pathlib.Path("run-outcome.json").exists()
                and json.loads(pathlib.Path("run-outcome.json").read_text()).get("status")
                in ("complete", "blocked")
            ):
                break
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Continue only the bounded worker assignment; save worker-result.json and stop when delivered, or preserve a real blocker."
                        if WORKER_TASK
                        else "Continue autonomously as coordinator; delegate concrete research, assess evidence and preserve state under the current goal. Check actual Host main identity before handoff."
                    ),
                }
            )
    except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
        failure = {
            "loop_error": type(e).__name__,
            "phase": getattr(e, "phase", loop_phase),
            "state": "checkpoint and journal retained",
            "time": time.time(),
        }
        if isinstance(e, context_manager.ContextError):
            failure["context_error"] = str(e)[:500]
        log(failure)
        try:
            context_manager.atomic("loop-failure.json", json.dumps(failure))
        except OSError:
            log({"failure_record_persistence": "unavailable"})
        break

# Let a running fork finish so the next process can apply its result, and apply
# one already-finished result before exiting.
try:
    compactor.wait(timeout=2)
    messages, final_record = compactor.apply_ready(messages)
    if final_record:
        log({"context_compaction": final_record})
        checkpoint(messages)
except Exception as e:  # noqa: BLE001 - shutdown must not lose the checkpoint
    log({"compaction_shutdown_error": type(e).__name__})
