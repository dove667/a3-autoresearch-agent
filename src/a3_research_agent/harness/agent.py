import hashlib
import http.client
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

from a3_research_agent.harness import state_paths

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from a3_research_agent.harness import (
    control_tools,
    deep_research,
    drift,
    file_tools,
    host_tools,
    job_failure,
    job_tools,
    ledger,
    lifecycle,
    operational_state,
    research_web,
    skill_registry,
    stop_request,
    tool_catalog,
    tool_contracts,
    tool_json,
    tool_results,
    waiting,
)
from a3_research_agent.harness import history as history_tools
from a3_research_agent.harness import (
    paths as runtime_paths,
)
from a3_research_agent.harness import (
    plan as planning,
)
from a3_research_agent.harness.context import compaction
from a3_research_agent.harness.context import facts as research_facts
from a3_research_agent.harness.context import manager as context_manager
from a3_research_agent.harness.context import recovery as context_recovery
from a3_research_agent.harness.memory import client as memory_client

BASE = None
TOKEN = None
PLAN_STALE_TURNS = 6
PLAN_PROGRESS_NOTICE = (
    "Check research_plan.next_action for the current focus. Record a meaningful result or "
    "decision using plan update or review. If the current batch is running, wait for its "
    "result. If a permission or resource prevents progress, record the blocker."
)
# Never let a macOS system proxy (for example Shadowrocket) intercept the
# loopback Host gateway. Only the model upstream needs its own direct route.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UPSTREAM_BAD_REQUEST_DELAYS = (5, 10)


class ModelRequestRejected(RuntimeError):
    def __init__(self, status: int, gateway_request_id: str | None):
        super().__init__(f"Model request rejected: HTTP {status}")
        self.status = status
        self.gateway_request_id = gateway_request_id


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
        with _OPENER.open(r, timeout=timeout) as x:
            return json.loads(x.read())
    except urllib.error.HTTPError as e:
        failure = {"http_error": e.code, "body": e.read().decode(errors="replace")[:10000]}
        if path == "/v1/responses" and e.headers is not None:
            failure["gateway_request_id"] = e.headers.get("X-Request-ID")
            failure["model_error"] = e.headers.get("X-A3-Model-Error")
        return failure


def request_model(request):
    """Retry model requests before publishing any tool calls."""
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    attempt, delay = 0, 5
    bad_request_retries = iter(UPSTREAM_BAD_REQUEST_DELAYS)
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
            gateway_request_id = response.get("gateway_request_id")
            if (
                status == 400
                and response.get("model_error") == "upstream_bad_request"
                and "tool_choice" not in request
                and (retry_delay := next(bad_request_retries, None)) is not None
            ):
                attempt += 1
                log(
                    {
                        "model_retry": attempt,
                        "delay_seconds": retry_delay,
                        "http_status": status,
                        "gateway_request_id": gateway_request_id,
                    }
                )
                time.sleep(retry_delay)
                continue
            if status not in retryable_statuses:
                raise ModelRequestRejected(status, gateway_request_id)
            failure = {"http_status": status}
        attempt += 1
        log({"model_retry": attempt, "delay_seconds": delay, **failure})
        time.sleep(delay)
        delay = min(120, delay * 2)


def log(x):
    state_paths.state_root().mkdir(parents=True, exist_ok=True)
    with open(state_paths.path(state_paths.TRAJECTORY), "a") as f:
        f.write(json.dumps(x, ensure_ascii=False) + "\n")


def request_host(method, path, data=None, *, timeout=300):
    """Keep HTTP transport and automatic observations inside the maintained runtime."""
    result = req(method, path, data, timeout=timeout)
    try:
        operational_state.record_gateway(method, path, result)
    except Exception:  # noqa: BLE001 - observation failure must not hide an executed operation
        try:
            log({"operational_receipt": "host_observation_failed"})
        except OSError:
            pass
    return result


def tool(name, arguments):
    if name in file_tools.NAMES:
        return file_tools.dispatch(name, arguments)
    if name in job_tools.NAMES:
        return job_tools.dispatch(name, arguments, request_host, cursor=WAIT_CURSOR)
    if name in host_tools.NAMES:
        return host_tools.dispatch(name, arguments, request_host)
    if name in control_tools.NAMES:
        return control_tools.dispatch(name, arguments, request_host, cursor=WAIT_CURSOR)
    if name in research_web.NAMES:
        return research_web.dispatch(name, arguments, request_host)
    if name == "deep_research":
        return deep_research.dispatch(arguments, request_host)
    if name == "skills":
        return skill_registry.dispatch(arguments)
    if name == "memory":
        return memory_client.dispatch(arguments)
    if name == "recall":
        return history_tools.recall_tool(arguments)
    if name == "plan":
        capabilities = (context_manager.TOKEN_CONFIG or {}).get("capabilities") or {}
        return planning.dispatch(arguments, require_research=capabilities.get("web") is True)
    tool_contracts.fail("unknown_tool", "Select a registered high-level research tool.")


def dispatch_tool_call(call):
    try:
        try:
            # A model sometimes leaks a protocol delimiter into the argument text;
            # that marker is unambiguous, so it is removed before failing the call.
            arguments = tool_json.loads(call.get("arguments", ""))
        except (ValueError, TypeError):
            return tool_results.failure(
                "invalid_tool_json",
                "Send one valid JSON object as arguments; do not include tool delimiters.",
            )
        if not isinstance(arguments, dict):
            return tool_results.failure(
                "invalid_arguments", "Tool arguments must be a JSON object."
            )
        contract = tool_catalog.BY_NAME.get(call.get("name"))
        if contract is None:
            return tool_results.failure(
                "unknown_tool",
                "Select a registered high-level tool; low-level gateway calls are unavailable.",
            )
        schema = contract["parameters"]
        tool_contracts.arguments(arguments, schema["properties"])
        missing = set(schema.get("required", [])) - arguments.keys()
        if missing:
            return tool_results.failure(
                "missing_arguments", "Provide required fields: " + ", ".join(sorted(missing))
            )
        return tool_results.normalize(tool(call["name"], arguments))
    except tool_contracts.ToolInputError as error:
        return tool_results.failure(error.code, error.hint)
    except FileNotFoundError:
        return tool_results.failure(
            "file_not_found", "List the directory and use an existing path."
        )
    except PermissionError:
        return tool_results.failure(
            "permission_denied", "Use an authorized research path or resource ID."
        )
    except Exception as error:  # noqa: BLE001 - unexpected implementation details stay private
        return tool_results.failure(
            "tool_execution_failed",
            "Report this tool failure with its arguments and stored response.",
            error_type=type(error).__name__,
        )


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
        phase = "research_receipt"
        research_facts.record(call, result)
        phase = "result_receipt"
        if call["name"] == "read":
            operational_state.record_page(call["call_id"], result)
            result = (
                operational_state.visible_page_reference(result, messages[:visible_count]) or result
            )
        if call["name"] in {"job_status", "job_wait"}:
            diagnosis = job_failure.diagnose(result)
            if diagnosis:
                result = {**result, "diagnosis": diagnosis}
        if call["name"] in {"job_wait", "idle_wait"}:
            operational_state.record_wait(result)
        ledger.from_tool(call, result)
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


tools = tool_catalog.TOOLS


INSTRUCTIONS = ""
compactor = None
WAIT_CURSOR = None
FORCE_SUPPORTED = True


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


def operational_message(*, observe_jobs=False):
    state = operational_state.load()
    plan = planning.load()
    if observe_jobs:
        operational_state.reconcile_jobs(req, plan, state)
    value = operational_state.view(state)
    value["frame"] = "CURRENT_STATE_REFRESH"
    value["state_authority"] = (
        "Current source refresh. Reconcile historical summaries against this plan revision, "
        "receipts and observed job states; do not repeat completed work or uncertain submissions."
    )
    value["research_plan"] = planning.view(plan, jobs=state.get("jobs", {}), compact=True)
    value["research_facts"] = research_facts.view(plan, state.get("jobs", {}))
    value["research_experience"] = memory_client.suggestions(value["research_plan"])
    value["relevant_skills"] = skill_registry.suggestions(value["research_plan"])
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
        research_facts.record(c, value)
        if c.get("name") == "read":
            operational_state.record_page(c["call_id"], value)
        if c.get("name") in {"job_wait", "idle_wait"}:
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
    global BASE, TOKEN, INSTRUCTIONS, compactor, WAIT_CURSOR, FORCE_SUPPORTED, tools
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
    if stale := stop_request.take():
        # A request this process never consumed would stop it on its first turn.
        log({"stale_stop_request_cleared": stale})
    empty = 0
    # Keep a drifting model inside real tool use instead of prose or repeat loops.
    guard = drift.DriftGuard()
    try:
        last_plan_revision = planning.load()["revision"]
    except Exception:  # noqa: BLE001 - a missing plan is handled by the normal tool path
        last_plan_revision = None
    plan_stale = 0
    log({"model_request_tools": tools})
    loop_phase = "decision_prepare"
    while True:
        try:
            loop_phase = "decision_prepare"
            lifecycle.self_status(req)
            try:
                context_manager.configure(req)
            except Exception as e:
                raise context_manager.ContextPending(
                    "Context configuration unavailable: " + type(e).__name__, retry_after=5
                ) from e
            configured = context_manager.TOKEN_CONFIG
            capabilities = configured.get("capabilities") if isinstance(configured, dict) else None
            tools = tool_catalog.enabled(capabilities)
            compactor.tools = tools
            operational = operational_message(observe_jobs=True)
            state_reference = context_manager.archive(operational, "operational-view")
            # Asynchronous fork: this decision keeps the full history; a finished
            # compaction result is applied before a later decision, never mid-flight.
            # The operational view rides outside history, so only its size is budgeted.
            prepared_messages, compaction_record = compactor.before_request(
                messages, extra_messages=[operational]
            )
            if compaction_record:
                log({"context_compaction": compaction_record})
                ledger.from_compaction(compaction_record)
            if prepared_messages is not messages:
                messages = prepared_messages
                # Delivered read pages are only "already in context" until the
                # history that carried them is rewritten.
                operational_state.reset_delivered()
                checkpoint(messages)
            context_recovery.clear(log=log)
            request = {
                "model": "openai/sandbox-model",
                "instructions": INSTRUCTIONS,
                "input": messages + [operational],
                "tools": tools,
            }
            if FORCE_SUPPORTED and guard.force_tool_choice():
                # Bounded: only after a text-only or rejected call, never by default.
                request["tool_choice"] = "required"
            try:
                req("POST", "/telemetry/observations", operational_state.counters(), timeout=2)
            except Exception:  # noqa: BLE001 — optional telemetry never blocks research
                log({"optional_telemetry": "unavailable"})
            log({"operational_view": state_reference})
            loop_phase = "model_request"
            try:
                resp = request_model(request)
            except RuntimeError as error:
                if FORCE_SUPPORTED and "tool_choice" in request and "HTTP 400" in str(error):
                    # A provider without forced tool choice must not stop research.
                    log({"tool_choice_unsupported": True})
                    FORCE_SUPPORTED = False
                    request.pop("tool_choice", None)
                    resp = request_model(request)
                else:
                    raise
            guard.spent()
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
            invalid_call = False
            for c in calls:
                loop_phase = "tool_execution"
                result = dispatch_tool_call(c)
                invalid_call = invalid_call or (
                    isinstance(result, dict)
                    and result.get("ok") is False
                    and result.get("error") in drift.INVALID_ARGUMENT_ERRORS
                )
                loop_phase = "result_persistence"
                save_tool_result(c, result, journal, messages, visible_messages)
            loop_phase = "checkpoint_publication"
            checkpoint(messages)
            context_manager.atomic(state_paths.path(state_paths.JOURNAL), "null")
            if stop := stop_request.take():
                # The turn is saved; stopping here leaves nothing half-applied.
                log({"operator_stop_requested": stop})
                log({"operator_stop_applied": "turn boundary"})
                break
            if "--test" in sys.argv and calls:
                break
            if last_plan_revision is not None:
                try:
                    current_plan = planning.load()
                except Exception:  # noqa: BLE001 - keep the research loop alive
                    current_plan = None
                if current_plan is not None:
                    if current_plan["revision"] != last_plan_revision:
                        last_plan_revision = current_plan["revision"]
                        plan_stale = 0
                    else:
                        plan_stale += 1
                        if plan_stale == PLAN_STALE_TURNS:
                            messages.append({"role": "user", "content": PLAN_PROGRESS_NOTICE})
                            log({"plan_progress_required": plan_stale})
            if not calls:
                if (
                    state_paths.path(state_paths.OUTCOME).exists()
                    and json.loads(state_paths.path(state_paths.OUTCOME).read_text()).get("status")
                    == "blocked"
                ):
                    break
                messages.append({"role": "user", "content": guard.observe_empty()})
                log({"model_drift": guard.snapshot()})
                checkpoint(messages)
            elif notice := guard.observe(calls, invalid_call):
                messages.append({"role": "user", "content": notice})
                log({"model_drift": guard.snapshot()})
                checkpoint(messages)
            if guard.stalled():
                log({"model_drift_exhausted": guard.snapshot(), "state": "corrections spent"})
                checkpoint(messages)
                break
        except context_manager.ContextError as error:
            try:
                context_recovery.wait(error, req, log=log)
            except (KeyboardInterrupt, lifecycle.ResearchStopped):
                log({"interrupted": True, "phase": "context_wait", "state": "operator stopped"})
                break
            continue
        except lifecycle.ResearchStopped:
            log({"interrupted": True, "phase": loop_phase, "state": "operator stopped"})
            break
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
            if isinstance(e, ModelRequestRejected):
                failure.update(http_status=e.status, gateway_request_id=e.gateway_request_id)
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
