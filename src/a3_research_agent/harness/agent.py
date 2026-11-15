import hashlib
import http.client
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

from a3_research_agent.harness import research_status, state_paths

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from a3_research_agent.harness import history as history_tools
from a3_research_agent.harness import (
    job_preflight,
    job_tools,
    lifecycle,
    long_term_memory,
    operational_state,
    planning,
    research_web,
    skill_registry,
    tool_contracts,
    waiting,
    workspace_tools,
)
from a3_research_agent.harness import (
    paths as runtime_paths,
)
from a3_research_agent.harness.context import compaction
from a3_research_agent.harness.context import manager as context_manager

BASE = None
TOKEN = None


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


def request_model(request):
    """Retry transient model transport failures before publishing tool calls."""
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    attempt, delay = 0, 5
    while True:
        try:
            # Allow the Host's upstream request to finish before the client times out.
            response = req("POST", "/v1/responses", request, timeout=960)
        except (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            http.client.HTTPException,
        ) as error:
            failure = {"error_type": type(error).__name__}
        else:
            status = response.get("http_error")
            if status is None:
                return response
            if status not in retryable_statuses:
                raise RuntimeError(f"Model request rejected: HTTP {status}")
            failure = {"http_status": status}
        attempt += 1
        log({"model_retry": attempt, "delay_seconds": delay, **failure})
        time.sleep(delay)
        delay = min(120, delay * 2)


def log(x):
    state_paths.state_root().mkdir(parents=True, exist_ok=True)
    with open(state_paths.path(state_paths.TRAJECTORY), "a") as f:
        f.write(json.dumps(x, ensure_ascii=False) + "\n")


def select_json_fields(raw, fields, path):
    if (
        not isinstance(fields, list)
        or not 1 <= len(fields) <= 32
        or any(not isinstance(k, str) or len(k) > 200 for k in fields)
    ):
        return {
            "error": "invalid_fields",
            "hint": "Use fields with 1..32 top-level JSON object keys, at most 200 characters each.",
        }
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {
            "error": "source_not_json",
            "path": path,
            "hint": "Use read without fields for a bounded text page; the source is not valid JSON.",
        }
    if not isinstance(value, dict):
        return {
            "error": "json_root_not_object",
            "path": path,
            "hint": "Use read without fields for a bounded page; fields selects top-level object keys.",
        }
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
    if name in job_tools.NAMES:
        return job_tools.dispatch(
            name,
            a,
            lambda method, path, body=None: tool(
                "gateway", {"method": method, "path": path, "body": body}
            ),
        )
    if name in workspace_tools.NAMES:
        return workspace_tools.dispatch(name, a)
    if name in research_web.NAMES:
        return research_web.dispatch(
            name,
            a,
            lambda method, path, body=None: tool(
                "gateway", {"method": method, "path": path, "body": body}
            ),
        )
    if name == "skills":
        return skill_registry.dispatch(a)
    if name == "recall":
        return history_tools.recall_tool(a)
    if name == "memory":
        return long_term_memory.memory_tool(a)
    if name == "preflight":
        return job_preflight.dispatch(
            {
                **a,
                "job": job_tools.submit_request(
                    a.get("job"), pathlib.Path.cwd(), allow_pinned_sources=True
                ),
            },
            lambda method, path, body=None: tool(
                "gateway", {"method": method, "path": path, "body": body}
            ),
        )
    if name == "plan":
        return planning.dispatch(a)
    if name == "progress":
        if a.get("action") == "outcome":
            return research_status.update(a)
        return operational_state.progress(a)
    if name == "gateway":
        path = a["path"]
        if not any(
            path == p or path.startswith((p + "/", p + "?"))
            for p in [
                "/jobs",
                "/resources/gpus",
                "/sessions",
                "/checks/comparison",
                "/fixtures/isolation",
                "/telemetry",
                "/vision",
                "/web",
            ]
        ):
            return "path not allowed"
        result = req(a["method"], path, a.get("body"))
        try:
            operational_state.record_gateway(a["method"], path, result)
        except Exception:  # noqa: BLE001 — preserve the result of an already executed operation
            try:
                log({"operational_receipt": "gateway_update_failed"})
            except OSError:
                pass  # Optional diagnostics cannot hide an executed operation.
        return result
    if name in ("read", "write", "list"):
        tool_contracts.text(a.get("path"), "path")
        p = pathlib.Path(a["path"]).resolve()
        roots = [
            runtime_paths.workspace_root(),
            runtime_paths.reference_root(),
            runtime_paths.input_root(),
            state_paths.state_root(),
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
            if not p.is_dir():
                return {
                    "error": "path_is_file",
                    "hint": "Use read for a file; list requires a directory.",
                }
            return [str(x) for x in p.iterdir()][:300]
        if p.is_dir():
            return {
                "error": "path_is_directory",
                "hint": "Use list for a directory, then read a selected file.",
            }
        if a.get("fields") is not None:
            if p.stat().st_size > 1024 * 1024:
                return {
                    "error": "field_selection_source_too_large",
                    "path": str(p),
                    "notice": "Use bounded offset/limit text pages; no JSON fields were read.",
                }
            return select_json_fields(p.read_bytes(), a["fields"], str(p))
        offset = tool_contracts.integer(a.get("offset", 0), "offset", 0, 2**63 - 1)
        limit = tool_contracts.integer(a.get("limit", 4000), "limit", 1, 8000)
        return operational_state.read_page(p, offset, limit)
    if name == "wait":
        seconds = a.get("seconds", 5)
        if type(seconds) is not int or not 0 <= seconds <= 600:
            raise ValueError("Wait seconds must be an integer in 0..600")
        jobs = waiting.active_jobs()
        if jobs and seconds:
            return waiting.wait_for_jobs(req, jobs, cursor=WAIT_CURSOR)
        return waiting.wait_for_change(
            req,
            seconds,
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
        try:
            arguments = json.loads(call["arguments"])
        except json.JSONDecodeError:
            return {
                "error": "invalid_tool_json",
                "hint": "Send one valid JSON object as the tool arguments.",
            }
        if not isinstance(arguments, dict):
            return {"error": "invalid_arguments", "hint": "Tool arguments must be a JSON object."}
        return tool(call["name"], arguments)
    except tool_contracts.ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
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


def save_tool_result(call, result, journal, messages, visible_count):
    """Persist the executed outcome before optional receipts or presentation work."""
    phase = "raw_result_journal"
    try:
        journal["results"][call["call_id"]] = result
        context_manager.atomic(
            state_paths.path(state_paths.JOURNAL), json.dumps(journal, ensure_ascii=False)
        )
        phase = "result_receipt"
        if call["name"] == "read":
            operational_state.record_page(call["call_id"], result)
            result = (
                operational_state.visible_page_reference(result, messages[:visible_count]) or result
            )
        if call["name"] == "wait":
            operational_state.record_wait(result)
        phase = "result_archival"
        bounded = context_manager.bound_tool_result(result, call)
        journal["results"][call["call_id"]] = bounded
        context_manager.atomic(
            state_paths.path(state_paths.JOURNAL), json.dumps(journal, ensure_ascii=False)
        )
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
        history_tools.append_history(value)
    except Exception as error:
        raise ToolResultPersistenceError(phase) from error


def spec(name, props, required):
    return {
        "type": "function",
        "name": name,
        "description": {
            "gateway": (
                "Advanced Host API access; prefer job_submit/job_status/job_artifact for common work. "
                "For remote job creation POST /jobs body "
                '{command:["python","-m","module","--help"],gpu:false,files:{}}; command MUST '
                "be an array. GET /jobs/{id} reads an accessible job's status and log tail. "
                "GET /resources/gpus reads aggregate GPU memory and utilization on demand. "
                "The current research workspace and artifacts are available for reading. Attach "
                "local scripts with workspace_files and existing datasets or model assets "
                "with input_exports. Use wait after independent submissions. Jobs execute "
                "isolated code, not local host commands."
            ),
            "read": "Read a bounded character page with content, whole-file SHA256, offset/end/total and EOF. limit is 1..8000, default 4000. An identical page still in context returns a reference. Optional fields selects top-level JSON keys instead.",
            "write": "Write text in workspace.",
            "list": "List accessible directory.",
            "preflight": (
                "Check a GPU job on CPU using the same command, source and inputs; workspace_files "
                "needs only path and optional destination, with source hashes computed locally. "
                "The check uses "
                "A3_RESEARCH_PREFLIGHT=1. Implement this mode in the entry to load the real "
                "assets, import dependencies, exercise a minimal forward or data read, write "
                "a3-preflight.json in the job workspace, and exit before production. After "
                "completion use action=submit with the same job and preflight_id."
            ),
            "plan": "Show or update the persistent research plan. Update with the current revision and patch steps by stable ID. Each new step needs task and criterion. Use depends_on for real prerequisites, jobs for submitted Controller IDs, progress for counts or blockers, and evidence when done. Give reason for route changes or archiving done/skipped steps. Show can select a single ID. Ready steps are included in the decision context; use wait when submitted work must finish.",
            "progress": (
                "Use list/update for operational items and workspace evidence. Use action=outcome "
                "with status=continue and reason to save research progress, or status=blocked with "
                "external_blocker (kind, requires_human=true, reason, required_action, evidence) "
                "when operator action is needed. Metadata is saved outside workspace. Closed "
                "items remain recorded across summaries."
            ),
            "wait": "Wait until an owned job finishes, fails or is cancelled, or an operator notification arrives. Queued and running jobs stay in program-level wait. With no jobs, seconds is a finite 0..600 delay.",
            "recall": (
                "Search original history by keyword; by default search all available history. "
                "Optionally restrict to an n..m round window (n=1 is the most recent round). "
                "Use after compaction or whenever "
                "earlier evidence is missing. Returns labelled snippets from the "
                "original messages. Default evidence mode skips tool arguments and retrieval echoes; "
                "use mode=raw for exact historical tool traffic."
            ),
            "memory": (
                "Append a durable note to project memory/entries.jsonl (action=write) or read/list "
                "entries (action=read|list). Search by keywords with action=search, key, "
                "and optional kind/scope filters. Write concise notes with refs and applicability "
                "in scope. Use supersedes with an earlier note ID when correcting it."
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


def strings(limit):
    return {"type": "array", "items": S, "maxItems": limit}


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
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
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
            "preflight",
            {
                "action": {"type": "string", "enum": ["check", "submit"]},
                "job": job_tools.PREFLIGHT_JOB,
                "preflight_id": S,
            },
            ["action", "job"],
        ),
        spec(
            "plan",
            {
                "action": {"type": "string", "enum": ["show", "update"]},
                "revision": {"type": "integer", "minimum": 0},
                "goal": S,
                "reason": S,
                "id": S,
                "archive": strings(planning.MAX_STEPS),
                "steps": {
                    "type": "array",
                    "maxItems": planning.MAX_STEPS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": S,
                            "task": S,
                            "criterion": S,
                            "status": {"type": "string", "enum": list(planning.STATUSES)},
                            "depends_on": strings(8),
                            "jobs": strings(8),
                            "progress": S,
                            "evidence": strings(4),
                        },
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                },
            },
            ["action"],
        ),
        spec(
            "progress",
            {
                "action": {"type": "string", "enum": ["list", "update", "outcome"]},
                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                "status": {
                    "type": "string",
                    "enum": ["open", "waiting", "closed", "continue", "blocked"],
                },
                "reason": {"type": "string", "maxLength": 1000},
                "external_blocker": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(research_status.EXTERNAL_BLOCKERS),
                        },
                        "requires_human": {"type": "boolean"},
                        "reason": {"type": "string", "maxLength": 1000},
                        "required_action": {"type": "string", "maxLength": 1000},
                        "evidence": strings(8),
                    },
                    "required": ["kind", "requires_human", "reason", "required_action", "evidence"],
                    "additionalProperties": False,
                },
                "note": {"type": "string", "maxLength": 400},
                "evidence": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                },
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
                "mode": {"type": "string", "enum": ["evidence", "raw"]},
                "window": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            ["keywords"],
        ),
        spec(
            "memory",
            {
                "action": {"type": "string", "enum": ["write", "read", "list", "search"]},
                "content": S,
                "key": S,
                "kind": S,
                "scope": S,
                "refs": strings(8),
                "supersedes": S,
            },
            ["action"],
        ),
    ]
    + job_tools.TOOLS
    + workspace_tools.TOOLS
    + research_web.TOOLS
    + skill_registry.TOOLS
)


INSTRUCTIONS = ""
compactor = None
WAIT_CURSOR = None


def save_manifest():
    files = set()
    manifests = {}
    for manifest in (state_paths.RESUME, state_paths.CONTINUATION):
        try:
            data = json.loads(state_paths.path(manifest).read_text())
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
            and not path.resolve().is_relative_to(state_paths.state_root())
            and "__pycache__" not in path.parts
            and not path.name.endswith(".tmp")
        ):
            files.add(path.as_posix())
    files = sorted(p for p in files if pathlib.Path(p).is_file())
    for filename, command in [
        (state_paths.RESUME, ["python", "-P", "-m", "a3_research_agent.harness.agent", "--resume"]),
        (
            state_paths.CONTINUATION,
            ["python", "-P", "-m", "a3_research_agent.harness.agent", "--resume"],
        ),
    ]:
        data = {**manifests[filename], "version": 1, "command": command, "files": files}
        context_manager.atomic(state_paths.path(filename), json.dumps(data))


def operational_message():
    state = operational_state.load()
    value = operational_state.view(state)
    value["research_plan"] = planning.view(jobs=state.get("jobs", {}), compact=True)
    value["runtime_paths"] = {
        "workspace": str(runtime_paths.workspace_root()),
        "state": str(state_paths.state_root()),
        "inputs": str(runtime_paths.input_root()),
        "reference": str(runtime_paths.reference_root()),
        "memory": str(runtime_paths.memory_root()),
        "skills": str(runtime_paths.skills_root()),
        "plan": str(runtime_paths.plan_file()),
    }
    return {"role": "user", "content": json.dumps(value, ensure_ascii=False)}


def checkpoint(messages):
    save_manifest()
    context_manager.atomic(
        state_paths.path(state_paths.CHECKPOINT), json.dumps(messages, ensure_ascii=False)
    )


def checkpoint_hash():
    return hashlib.sha256(state_paths.path(state_paths.CHECKPOINT).read_bytes()).hexdigest()


def recover_journal(messages):
    p = state_paths.path(state_paths.JOURNAL)
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
            operational_state.record_page(c["call_id"], value)
        if c.get("name") == "wait":
            operational_state.record_wait(value)
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
    return restored


def main() -> None:
    """Initialize one explicit execution; importing this module never starts it."""
    global BASE, TOKEN, INSTRUCTIONS, compactor, WAIT_CURSOR
    BASE = os.environ["A3_GATEWAY_URL"]
    TOKEN = os.environ["A3_GATEWAY_TOKEN"]
    if "--worker-task" in sys.argv:
        raise ValueError("Worker entry points were removed; use the single research Agent")
    INSTRUCTIONS = lifecycle.INSTRUCTIONS
    compactor = compaction.ForkCompactor(req, tools=list(tools))
    messages = json.loads(state_paths.path(state_paths.CHECKPOINT).read_text())
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise ValueError("Research checkpoint must be a message list")
    try:
        messages = recover_journal(messages)
        context_manager.restore_artifact_views(messages)
        history_tools.ensure_history(messages)
        checkpoint(messages)
    except Exception as e:  # noqa: BLE001 - preserve recoverable state on unexpected failures
        log({"recovery_error": str(e)})
        sys.exit(1)
    WAIT_CURSOR = waiting.wait_cursor(messages)
    empty = 0
    log({"model_request_tools": tools})
    loop_phase = "decision_prepare"
    while True:
        try:
            loop_phase = "decision_prepare"
            lifecycle.self_status(req)
            try:
                context_manager.configure(req)
            except Exception as e:  # noqa: BLE001 - preserve the checkpoint when Host budgets are unknown
                log({"context_config_error": type(e).__name__, "state": "checkpoint preserved"})
                context_manager.atomic(
                    state_paths.path(state_paths.CONTEXT_FAILURE),
                    json.dumps(
                        {
                            "error": "context_config_unavailable",
                            "state": "original checkpoint preserved",
                        }
                    ),
                )
                save_manifest()
                break
            operational = operational_message()
            state_reference = context_manager.archive(operational, "operational-view")
            # Asynchronous fork: this decision keeps the full history; a finished
            # compaction result is applied before a later decision, never mid-flight.
            # The operational view rides outside history, so only its size is budgeted.
            prepared_messages, compaction_record = compactor.before_request(
                messages, extra_messages=[operational]
            )
            if compaction_record:
                log({"context_compaction": compaction_record})
            if prepared_messages is not messages:
                messages = prepared_messages
                checkpoint(messages)
            request = {
                "model": "openai/sandbox-model",
                "instructions": INSTRUCTIONS,
                "input": messages + [operational],
                "tools": tools,
            }
            try:
                req("POST", "/telemetry/observations", operational_state.counters(), timeout=2)
            except Exception:  # noqa: BLE001 — optional telemetry never blocks research
                log({"optional_telemetry": "unavailable"})
            log({"operational_view": state_reference})
            loop_phase = "model_request"
            resp = request_model(request)
            if resp.get("status") == "completed":
                operational_state.mark_delivered(messages)
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
            context_manager.atomic(
                state_paths.path(state_paths.JOURNAL), json.dumps(journal, ensure_ascii=False)
            )
            # All waits in this response use the pre-decision cursor: a tool result
            # is not yet a normal model decision observing the notification.
            WAIT_CURSOR = waiting.wait_cursor(messages)
            visible_messages = len(messages)
            messages.extend(out)
            history_tools.append_history_many(out)
            for c in calls:
                loop_phase = "tool_execution"
                result = dispatch_tool_call(c)
                loop_phase = "result_persistence"
                save_tool_result(c, result, journal, messages, visible_messages)
            loop_phase = "checkpoint_publication"
            checkpoint(messages)
            context_manager.atomic(state_paths.path(state_paths.JOURNAL), "null")
            if "--test" in sys.argv and calls:
                break
            if not calls:
                if (
                    state_paths.path(state_paths.OUTCOME).exists()
                    and json.loads(state_paths.path(state_paths.OUTCOME).read_text()).get("status")
                    == "blocked"
                ):
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue the research yourself: implement, submit independent jobs in "
                            "parallel, inspect results and repair ordinary script errors in this task. "
                            "Preserve experiment evidence and act on the most informative next research step."
                        ),
                    }
                )
                checkpoint(messages)
        except KeyboardInterrupt:
            log(
                {
                    "interrupted": True,
                    "phase": loop_phase,
                    "state": "last paired checkpoint and pending-call journal retained",
                }
            )
            break
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
                context_manager.atomic(
                    state_paths.path(state_paths.LOOP_FAILURE), json.dumps(failure)
                )
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


if __name__ == "__main__":
    main()
