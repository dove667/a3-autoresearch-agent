"""Models Gateway behavior on the shared runtime instance."""

import hashlib
import json
import time
from copy import deepcopy
from typing import Annotated

from fastapi import Header, HTTPException

from a3_research_agent.host.models.settings import (
    validate_model_request_options,
)
from a3_research_agent.host.models.vision import prepare_vision_request
from a3_research_agent.host.telemetry.diagnostics import CURRENT_REQUEST
from a3_research_agent.token_count import count_tokens, estimate_request_tokens

from .instructions import MODEL_FIELDS, SUMMARY_INSTRUCTIONS
from .schemas import (
    SummaryRequest,
)


class ModelRoutes:
    def forward_response(
        self,
        body: dict,
        authorization,
        *,
        purpose: str = "decision",
        summary_refs=None,
        vision_image_reserve: int = 0,
        vision_frames=None,
    ):
        self.accepting_resources()
        experiment = self.owner(authorization)
        if set(body) - MODEL_FIELDS:
            raise HTTPException(422, "Unsupported model parameter")
        if body.get("stream"):
            raise HTTPException(422, "Streaming forwarding is not enabled")
        tools = body.get("tools") or []
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
        ):
            raise HTTPException(422, "Only client-executed function tools are supported")
        arguments = {key: value for key, value in body.items() if key != "model"}
        arguments.pop("stream", None)
        if arguments.get("instructions") is None:
            arguments.pop("instructions", None)
        elif not isinstance(arguments["instructions"], str):
            raise HTTPException(422, "Instructions must be text or null")
        with self.session_lock:
            trial = self.evolution.trials.get(experiment, {}) if self.evolution else {}
            receives_phase = bool(
                purpose == "decision"
                and (self.phase_instructions or self.input_versions)
                and self.evolution
                and (experiment == self.evolution.main_owner or trial.get("status") == "testing")
            )
        role = (
            "summary"
            if purpose == "summary"
            else "worker"
            if purpose == "vision"
            else "candidate"
            if trial.get("status") == "testing"
            else "main"
            if self.evolution is None
            or experiment == self.evolution.main_owner
            or experiment in self.root_owners
            else "worker"
        )
        route = self.fixed_roles.get(
            role, {"model_name": self.model_name, "request_options": self.fixed_model_options}
        )
        options = route["request_options"]
        arguments.update(validate_model_request_options(options))
        input_manifest, active_phase = (
            self.input_versions.model_instructions()
            if self.input_versions is not None
            else ({}, self.phase_instructions)
        )
        if receives_phase:
            specific = arguments.get("instructions")
            if specific is not None and not isinstance(specific, str):
                raise HTTPException(422, "Instructions must be text")
            arguments["instructions"] = (
                active_phase + "\n\n" + specific if specific else active_phase
            )
        if isinstance(arguments.get("input"), str):
            arguments["input"] = [{"role": "user", "content": arguments["input"]}]
        feedback_ids = []
        if self.human_feedback is not None and purpose == "decision":
            try:
                arguments, feedback_ids = self.human_feedback.apply(experiment, arguments)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        estimated_tokens = None
        if self.token_budget:
            estimate_arguments = arguments
            if purpose == "vision":
                # Binary transport length is not an image token count. Use a bounded,
                # deliberately conservative per-image admission reserve instead.
                estimate_arguments = deepcopy(arguments)
                for message in estimate_arguments.get("input", []):
                    if isinstance(message.get("content"), list):
                        message["content"] = [
                            part for part in message["content"] if part.get("type") != "input_image"
                        ]
            estimated_tokens = (
                estimate_request_tokens(
                    estimate_arguments, self.token_budget.provider_reserve_tokens
                )
                + vision_image_reserve
            )
            window = (
                self.token_budget.summary_context_tokens
                if role == "summary"
                else self.token_budget.worker_context_tokens
                if role == "worker"
                else self.token_budget.main_context_tokens
            )
            limit = (
                self.token_budget.summary_input_tokens
                if role == "summary"
                else window * 9 // 10 - self.token_budget.output_reserve_tokens
            )
            if estimated_tokens > limit:
                raise HTTPException(422, "Estimated model input exceeds configured token headroom")
        started = time.monotonic()
        try:
            response = self.forward_model(model=route["model_name"], **arguments)
            data = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
            request = CURRENT_REQUEST.get()
            if request is not None:
                usage = data.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                request["model_observation"] = {
                    "role": role,
                    "wall_seconds": time.monotonic() - started,
                    **{
                        key: usage.get(key)
                        for key in ("input_tokens", "output_tokens", "total_tokens")
                    },
                }
        except Exception as error:  # noqa: BLE001 — never return provider or SSH credential details
            request = CURRENT_REQUEST.get()
            if request is not None:
                request["model_observation"] = {
                    "role": role,
                    "wall_seconds": time.monotonic() - started,
                }
            self.diagnostics.error("model_provider", error)
            raise HTTPException(502, "Model forwarding failed") from None
        if self.human_feedback is not None:
            self.human_feedback.forwarded(feedback_ids)
        entry = {
            "experiment_id": experiment,
            "model": route["model_name"],
            "model_role": role,
            "estimated_input_tokens": estimated_tokens,
            "prepared_instruction_bytes": len(arguments.get("instructions", "").encode()),
            "prepared_input_bytes": len(
                json.dumps(arguments.get("input", []), ensure_ascii=False).encode()
            ),
            "wall_seconds": time.monotonic() - started,
            "usage": data.get("usage"),
        }
        if options:
            entry["model_request_options"] = options
        entry["response_model"] = data.get("model")
        reasoning = data.get("reasoning")
        entry["response_reasoning_effort"] = (
            reasoning.get("effort") if isinstance(reasoning, dict) else None
        )
        entry["response_service_tier"] = data.get("service_tier")
        if purpose == "summary":
            entry["summary_feedback_refs"] = summary_refs or []
        if purpose == "vision":
            entry.update(
                model_purpose="vision",
                vision_frame_count=len(vision_frames or []),
                vision_frames=vision_frames or [],
                image_admission_reserve_tokens=vision_image_reserve,
            )
        if feedback_ids:
            entry["human_feedback_ids"] = feedback_ids
        if receives_phase:
            entry["phase_instructions_sha256"] = hashlib.sha256(active_phase.encode()).hexdigest()
        if input_manifest:
            entry["input_revision"] = input_manifest["revision"]
            entry["input_versions"] = input_manifest["files"]
        with self.log_lock:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_log.open("a") as stream:
                stream.write(json.dumps(entry) + "\n")
        if input_manifest:
            from fastapi.responses import JSONResponse

            return JSONResponse(data, headers={"X-A3-Input-Revision": input_manifest["revision"]})
        return data

    def context_configuration(self, authorization: Annotated[str | None, Header()] = None):
        self.accepting_resources()
        experiment = self.owner(authorization)
        with self.session_lock:
            trial = self.evolution.trials.get(experiment, {}) if self.evolution else {}
            receives_phase = bool(
                self.evolution
                and (experiment == self.evolution.main_owner or trial.get("status") == "testing")
            )
        phase = (
            self.input_versions.model_instructions()[1]
            if self.input_versions is not None
            else self.phase_instructions
        )
        records = [
            record
            for record in (self.human_feedback.records if self.human_feedback else [])
            if record["owner"] == experiment
        ]
        notes = [self.human_feedback.message(record) for record in records]
        refs = [
            {"feedback_id": record["feedback_id"], "sha256": record["sha256"]} for record in records
        ]
        injected = {"instructions": phase if receives_phase else "", "feedback": notes}
        main_route = (
            self.evolution is None
            or experiment == self.evolution.main_owner
            or experiment in self.root_owners
            or trial.get("status") == "testing"
        )
        window = (
            self.token_budget.main_context_tokens
            if main_route
            else self.token_budget.worker_context_tokens
        )
        return {
            "enabled": True,
            "summary_operation": "/v1/summary",
            "unit": "estimated_tokens",
            "tokenizer": "o200k_base",
            "trigger_tokens": self.token_budget.trigger_tokens,
            "decision_input_tokens": window * 9 // 10 - self.token_budget.output_reserve_tokens,
            "target_tokens": self.token_budget.target_tokens,
            "summary_input_tokens": self.token_budget.summary_input_tokens,
            "injected_reserve_tokens": count_tokens(injected)
            + self.token_budget.provider_reserve_tokens
            + 512
            + 16 * len(notes),
            "summary_reserve_tokens": count_tokens(SUMMARY_INSTRUCTIONS)
            + count_tokens(refs)
            + self.token_budget.provider_reserve_tokens
            + 256,
            "main_context_tokens": self.token_budget.main_context_tokens,
            "summary_context_tokens": self.token_budget.summary_context_tokens,
            "feedback_refs": refs,
            "model_routes_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "main": self.model_name,
                        "main_options": self.fixed_model_options,
                        "roles": self.fixed_roles,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        }

    def responses(self, body: dict, authorization: Annotated[str | None, Header()] = None):
        return self.forward_response(body, authorization)

    def summarize(
        self, body: SummaryRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.accepting_resources()
        experiment = self.owner(authorization)
        references = [
            {"feedback_id": record["feedback_id"], "sha256": record["sha256"]}
            for record in (self.human_feedback.records if self.human_feedback else [])
            if record["owner"] == experiment
        ]
        payload = {
            "source": body.source,
            "transcript": body.transcript,
            "operator_feedback_refs": references,
        }
        result = self.forward_response(
            {
                "instructions": SUMMARY_INSTRUCTIONS,
                "input": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "max_output_tokens": 1000,
            },
            authorization,
            purpose="summary",
            summary_refs=references,
        )
        if not isinstance(result, dict):
            # Input revision headers are not needed for a summary data response.
            result = json.loads(result.body)
        return {**result, "a3_summary": {"source": body.source, "feedback_refs": references}}

    def vision_status(self, authorization: Annotated[str | None, Header()] = None):
        self.owner(authorization)
        return {
            "available": True,
            "operation": "/vision/inspect",
            "max_frames": 6,
            "max_image_bytes": 512 * 1024,
            "max_total_bytes": 3 * 1024 * 1024,
            "max_side_pixels": 1024,
            "model_role": "worker",
            "source": "authorized own-job PNG/JPEG artifacts; not arbitrary URLs",
            "note": "Transport available; actual provider vision requires a successful image response",
        }

    def inspect_vision(self, body: dict, authorization: Annotated[str | None, Header()] = None):
        self.accepting_resources()
        actor = self.owner(authorization)
        if not self.vision_slots.acquire(blocking=False):
            raise HTTPException(429, "Visual inspection busy; no model request was dispatched")
        try:

            def read_frame(frame_owner, job_id, path):
                return self.remote(
                    "artifact",
                    {
                        "experiment_id": self.job_owner(frame_owner, job_id, shared_read=True),
                        "job_id": job_id,
                        "path": path,
                        "max_bytes": 512 * 1024,
                    },
                )

            try:
                arguments, metadata, reserve = prepare_vision_request(body, actor, read_frame)
            except (ValueError, TypeError):
                raise HTTPException(
                    422, "Invalid frame input, image, size or chronological labels"
                ) from None
            result = self.forward_response(
                arguments,
                authorization,
                purpose="vision",
                vision_image_reserve=reserve,
                vision_frames=metadata,
            )
            if not isinstance(result, dict):
                result = json.loads(result.body)
            text = "".join(
                part.get("text", "")
                for message in result.get("output", [])
                if message.get("type") == "message"
                for part in message.get("content", [])
                if part.get("type") == "output_text"
            )
            if result.get("status") != "completed" or not text.strip() or len(text) > 16000:
                raise HTTPException(
                    502,
                    "Visual response incomplete, missing or oversized; no observation confirmed",
                )
            reasoning = result.get("reasoning") or {}
            return {
                "status": "completed",
                "analysis": text,
                "frames": metadata,
                "model": result.get("model"),
                "reasoning_effort": reasoning.get("effort")
                if isinstance(reasoning, dict)
                else None,
                "scope": "Visual-helper observations, hypotheses and unknowns; not scientific acceptance",
            }
        finally:
            self.vision_slots.release()
