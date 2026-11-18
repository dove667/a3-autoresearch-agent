"""Background summaries with resumable waiting at the decision-budget boundary.

Temporary failures retain a scheduled retry. Decisions that still fit may proceed;
oversized decisions yield control to the run loop until a valid summary is ready.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time
import uuid

from a3_research_agent.harness import history as history_tools
from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as cm

RESULT_PATH = state_paths.COMPACTION_RESULT
EPOCH_PATH = state_paths.COMPACTION_EPOCH
CHECKPOINT_OPEN = cm.MEMORY_OPEN
CHECKPOINT_CLOSE = cm.MEMORY_CLOSE


def digest(messages):
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ForkCompactor:
    """One durable, prefix-bound fork; replay publication, never unknown calls."""

    def __init__(
        self,
        req,
        *,
        tools=None,
        trigger_tokens=None,
        result_path=RESULT_PATH,
        clock=time.time,
    ):
        self.req, self.tools = req, list(tools or [])
        self.trigger_tokens = trigger_tokens
        self.result_path = (
            state_paths.path(result_path)
            if not pathlib.Path(result_path).is_absolute()
            else pathlib.Path(result_path)
        )
        self.clock = clock
        self._thread, self._lock = None, threading.Lock()
        self.last_error, self._last_after_bytes = None, 0
        self._last_after_tokens = 0

    def _epoch(self):
        path = state_paths.path(EPOCH_PATH)
        value = json.loads(path.read_text()) if path.exists() else {"version": 1, "id": None}
        if value.get("version") != 1:
            raise cm.ContextError("Invalid compaction epoch; explicit recovery required")
        if value.get("id") is None:
            value["id"] = uuid.uuid4().hex
            cm.atomic(path, json.dumps(value))
        if not isinstance(value["id"], str) or len(value["id"]) != 32:
            raise cm.ContextError("Invalid compaction epoch identity")
        return value["id"]

    def _budget(self, extra_messages=None):
        budget = cm.decision_budget(self.tools)
        if extra_messages:
            budget -= cm.message_cost(extra_messages)
        if budget <= 0 and not cm.TOKEN_CONFIG.get("provider_token_usage"):
            raise cm.ContextError("Operational/tool reserves exhaust decision budget")
        return max(1, budget)

    def _target_budget(self, budget):
        extra_cost = max(0, cm.decision_budget(self.tools) - budget)
        target = (
            cm.TOKEN_CONFIG.get("target_tokens", budget)
            - cm.TOKEN_CONFIG.get("injected_reserve_tokens", 0)
            - cm.metric(self.tools)
            - 16 * len(self.tools)
            - extra_cost
        )
        return min(budget, max(1, target))

    def before_request(
        self, messages, *, extra_messages=None, observed_input_tokens=None, force=False
    ):
        if not cm.paired(messages):
            raise cm.ContextError("Unsafe tool call/result boundary")
        budget = self._budget(extra_messages)
        provider_usage = cm.TOKEN_CONFIG.get("provider_token_usage")
        candidate, record = self._apply_if_ready(messages, budget)
        if record and record.get("kind") == "applied":
            if not provider_usage and cm.message_cost(candidate) > budget:
                raise cm.ContextError(
                    "Compaction result exceeds current decision reserves; originals retained"
                )
            return candidate, record
        if record and (record.get("kind") == "blocked" or record.get("blocked")):
            raise cm.ContextPending(
                record["reason"], retry_after=record.get("retry_after", 30), blocked=True
            )
        if force or (not provider_usage and cm.message_cost(messages) > budget):
            if not self._busy() and not record:
                self.start(messages, extra_messages=extra_messages, budget=budget)
            raise cm.ContextPending(
                (record or {}).get("reason", "summary_pending"),
                retry_after=(record or {}).get("retry_after", 1),
            )
        if self._busy() or record and record.get("kind") in {"retry_wait", "fork_resumed"}:
            return messages, record
        trigger = (
            self.trigger_tokens
            if self.trigger_tokens is not None
            else cm.TOKEN_CONFIG["trigger_tokens"]
        )
        if provider_usage:
            size, floor = observed_input_tokens or 0, trigger
        else:
            trigger -= cm.TOKEN_CONFIG["decision_input_tokens"] - budget
            size, floor = (
                cm.message_cost(messages),
                max(trigger, int(self._last_after_tokens * 1.1)),
            )
        if size >= floor:
            return messages, self.start(messages, extra_messages=extra_messages, budget=budget)
        return messages, record

    def apply_ready(self, messages, *, extra_messages=None):
        """Shutdown/recovery publication only: never start a fork or a model call."""
        if not cm.paired(messages):
            raise cm.ContextError("Unsafe tool call/result boundary")
        candidate, record = self._apply_if_ready(
            messages, self._budget(extra_messages), resume=False
        )
        if (
            not cm.TOKEN_CONFIG.get("provider_token_usage")
            and record
            and record.get("kind") == "applied"
            and cm.message_cost(candidate) > self._budget(extra_messages)
        ):
            raise cm.ContextError(
                "Compaction result exceeds current decision reserves; originals retained"
            )
        return candidate, record

    def start(self, messages, *, extra_messages=None, budget=None):
        if self._busy():
            raise cm.ContextError("Compaction already in progress")
        snapshot = json.loads(json.dumps(messages, ensure_ascii=False))
        if not cm.paired(snapshot):
            raise cm.ContextError("Unsafe compaction source")
        history_tools.ensure_history(snapshot)
        source = cm.archive(snapshot, "snapshot")
        record = {
            "version": 2,
            "kind": "fork_started",
            "status": "running",
            "applied": False,
            "fork_id": uuid.uuid4().hex,
            "epoch": self._epoch(),
            "prefix_length": len(snapshot),
            "prefix_sha256": digest(snapshot),
            "source": source,
            "snapshot_bytes": cm.size(snapshot),
            "state_refresh": json.loads(json.dumps(extra_messages or [], ensure_ascii=False)),
        }
        cm.atomic(self.result_path, json.dumps(record))
        hard_budget = budget if budget is not None else self._budget(extra_messages)
        self._launch(record, snapshot, hard_budget)
        return {key: value for key, value in record.items() if key not in {"replacement", "tail"}}

    def _launch(self, record, snapshot, budget):
        with self._lock:
            self._thread = threading.Thread(
                target=self._run,
                args=(record, snapshot, budget),
                name="compaction-fork",
                daemon=True,
            )
            self._thread.start()

    def _busy(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def _read_result(self):
        if not self.result_path.exists():
            return None
        try:
            result = json.loads(self.result_path.read_text())
        except (OSError, ValueError) as error:
            raise cm.ContextError(
                "Invalid persisted compaction result; preserve for inspection"
            ) from error
        if result is None:
            return None
        if not isinstance(result, dict):
            raise cm.ContextError("Invalid persisted compaction result shape")
        return result

    def status(self):
        result = self._read_result()
        return {
            "busy": self._busy(),
            "pending": bool(result and not result.get("applied")),
            "result": {
                key: value
                for key, value in (result or {}).items()
                if key not in {"replacement", "tail", "checkpoint"}
            },
        }

    def _run(self, record, snapshot, budget):
        result = dict(record)
        try:
            prepared, progress = cm.prepare(
                snapshot,
                self.tools,
                self.req,
                budget_limit=budget,
                force_summary=True,
                extra_messages=record.get("state_refresh", []),
                summary_attempt_id=record.get("summary_attempt_id"),
            )
            if progress is None:
                raise cm.ContextError("Soft fork produced no compression")
            replacement = list(prepared)
            result.pop("blocked", None)
            result.update(status="ready", strategy="host_summary", replacement=replacement)
        except Exception as error:  # noqa: BLE001 — a failed summary cannot replace research history.
            self.last_error = type(error).__name__
            for name in ("replacement", "replacement_sha256", "after_bytes"):
                result.pop(name, None)
            if isinstance(error, cm.SummaryRetry):
                result.update(
                    status="retry_wait",
                    reason=error.reason,
                    retry_after=error.retry_after,
                    next_retry_at=self.clock() + error.retry_after,
                    retry_count=result.get("retry_count", 0) + 1,
                    blocked=error.blocked,
                )
            elif isinstance(error, cm.ContextFitError):
                result.update(
                    status="retry_wait",
                    reason=str(error)[:500],
                    retry_after=30,
                    next_retry_at=self.clock() + 30,
                    retry_count=result.get("retry_count", 0) + 1,
                    summary_attempt_id=uuid.uuid4().hex,
                    blocked=False,
                )
            else:
                result.update(
                    status="blocked",
                    reason=str(error)[:500],
                    blocked_context=self._context_signature(budget),
                )
            result["error_type"] = type(error).__name__
        if "replacement" in result:
            result["replacement_sha256"] = digest(result["replacement"])
            result["after_bytes"] = cm.size(result["replacement"])
        cm.atomic(self.result_path, json.dumps(result, ensure_ascii=False))

    def _restore_trigger_baseline(self, result, messages):
        # Bind the baseline to the whole published candidate, including any tail
        # produced while the summary ran. Recompute in the current counting unit.
        baseline = result.get("trigger_baseline", {})
        count = baseline.get("length") if isinstance(baseline, dict) else None
        if (
            type(count) is int
            and 0 < count <= len(messages)
            and digest(messages[:count]) == baseline.get("sha256")
        ):
            published = messages[:count]
        else:
            return
        self._last_after_bytes = cm.size(published)
        self._last_after_tokens = cm.message_cost(published)

    def _context_signature(self, budget):
        # A new fitting algorithm can recover a previously blocked source.
        return digest({"token_contract": cm.TOKEN_CONFIG, "budget": budget, "fit_version": 2})

    def _restart(self, result, archived, budget):
        running = {**result, "status": "running"}
        for field in ("reason", "error_type", "retry_after", "next_retry_at", "blocked_context"):
            running.pop(field, None)
        cm.atomic(self.result_path, json.dumps(running, ensure_ascii=False))
        self._launch(running, archived, budget)
        return {
            "kind": "fork_resumed",
            "source": result["source"],
            "blocked": bool(result.get("blocked")),
            "reason": "summary_probe_pending",
        }

    def _apply_if_ready(self, messages, budget=None, *, resume=True):
        result = self._read_result()
        if not result:
            return messages, None
        if result.get("applied"):
            self._restore_trigger_baseline(result, messages)
            return messages, None
        valid = result.get("version") == 2 and result.get("epoch") == self._epoch()
        active = result.get("publication", result)
        if not isinstance(active, dict):
            raise cm.ContextError("Invalid bounded publication state")
        replacement = active.get("replacement")
        if (
            valid
            and isinstance(replacement, list)
            and digest(replacement) == active.get("replacement_sha256")
            and len(messages) >= len(replacement)
            and digest(messages[: len(replacement)]) == active["replacement_sha256"]
        ):
            self._restore_trigger_baseline(result, messages)
            checkpoint = state_paths.path(state_paths.CHECKPOINT)
            if checkpoint.exists():
                saved = json.loads(checkpoint.read_text())
                if (
                    len(saved) >= len(replacement)
                    and digest(saved[: len(replacement)]) == active["replacement_sha256"]
                ):
                    cm.atomic(
                        self.result_path,
                        json.dumps({**result, "applied": True}, ensure_ascii=False),
                    )
            return messages, None
        count = active.get("prefix_length")
        matches = (
            valid
            and type(count) is int
            and 0 <= count <= len(messages)
            and digest(messages[:count]) == active.get("prefix_sha256")
        )
        if not matches:
            cm.archive(result, "rejected-compaction-result")
            cm.atomic(
                self.result_path,
                json.dumps({**result, "applied": True, "rejected": "source_identity_mismatch"}),
            )
            return messages, {"kind": "apply_skipped", "reason": "source_identity_mismatch"}
        try:
            archived = json.loads(state_paths.path(active["source"]).read_text())
        except (KeyError, OSError, ValueError) as error:
            raise cm.ContextError("Compaction source archive missing or invalid") from error
        if digest(archived) != active["prefix_sha256"]:
            raise cm.ContextError("Compaction source archive changed")
        budget = self._budget() if budget is None else budget
        if active.get("status") == "running":
            if self._busy() or not resume:
                return messages, (
                    {
                        "kind": "retry_wait",
                        "reason": "summary_probe_pending",
                        "blocked": True,
                        "retry_after": 1,
                    }
                    if active.get("blocked")
                    else None
                )
            return messages, self._restart(result, archived, budget)
        if active.get("status") == "retry_wait":
            delay = max(0, active["next_retry_at"] - self.clock())
            if delay == 0 and resume and not self._busy():
                return messages, self._restart(result, archived, budget)
            return messages, {
                "kind": "retry_wait",
                "reason": active["reason"],
                "retry_after": max(1, delay),
                "next_retry_at": active["next_retry_at"],
                "blocked": bool(active.get("blocked")),
            }
        if active.get("status") == "blocked":
            signature = self._context_signature(budget)
            if active.get("blocked_context") != signature and resume and not self._busy():
                return messages, self._restart(result, archived, budget)
            return messages, {"kind": "blocked", "reason": active.get("reason", "summary_blocked")}
        if active.get("status") == "fallback" or active.get("strategy") in {
            "archive_fallback",
            "interrupted",
        }:
            cm.archive(result, "rejected-compaction-result")
            cm.atomic(
                self.result_path,
                json.dumps({**result, "applied": True, "rejected": "complete_summary_required"}),
            )
            return messages, {"kind": "apply_skipped", "reason": "complete_summary_required"}
        replacement = active.get("replacement")
        if (
            not isinstance(replacement, list)
            or digest(replacement) != active.get("replacement_sha256")
            or not cm.paired(replacement)
        ):
            raise cm.ContextError("Invalid paired compaction replacement")
        candidate = replacement + messages[count:]
        if not cm.paired(candidate):
            raise cm.ContextError("Compaction would orphan tool results")
        budget = self._budget() if budget is None else budget
        soft_target = self._target_budget(budget)
        if not cm.TOKEN_CONFIG.get("provider_token_usage") and cm.message_cost(candidate) > budget:
            newest = messages[count:]
            old_tail = replacement[1:]
            remaining = budget - cm.message_cost([replacement[0]] + newest)
            cut = (
                cm._token_tail_boundary(old_tail, remaining, len(old_tail))
                if remaining > 0
                else None
            )
            candidate = [replacement[0]] + (old_tail[cut:] if cut is not None else []) + newest
            fitted = cm.fit_tool_outputs(candidate, budget, start_index=1)
            # The proposal covers the exact current prefix, including its new tail.
            # Persist before checkpoint publication so either side of a crash replays
            # this representation once, without another summary or duplicated calls.
            publication = {
                "prefix_length": len(messages),
                "prefix_sha256": digest(messages),
                "source": cm.archive(messages, "snapshot"),
                "status": "ready",
                "replacement": fitted,
                "replacement_sha256": digest(fitted),
                "after_bytes": cm.size(fitted),
                "strategy": "bounded_merge",
            }
            result["publication"] = publication
            result["after_bytes"] = publication["after_bytes"]
            cm.atomic(self.result_path, json.dumps(result, ensure_ascii=False))
            candidate, active = fitted, publication
        baseline = {"length": len(candidate), "sha256": digest(candidate)}
        if result.get("trigger_baseline") != baseline:
            result["trigger_baseline"] = baseline
            cm.atomic(self.result_path, json.dumps(result, ensure_ascii=False))
        self._restore_trigger_baseline(result, candidate)
        return candidate, {
            "kind": "applied",
            "fork_id": result["fork_id"],
            "status": active["status"],
            "strategy": active["strategy"],
            "before_bytes": cm.size(messages),
            "after_bytes": cm.size(candidate),
            "soft_target_exceeded": cm.message_cost(candidate) > soft_target,
        }
