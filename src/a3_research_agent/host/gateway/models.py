"""Models Gateway behavior on the shared runtime instance."""

import hashlib
import json
import re
import time
from copy import deepcopy
from typing import Annotated

from fastapi import Header, HTTPException

from a3_research_agent.harness import tool_json
from a3_research_agent.host.models import pricing
from a3_research_agent.host.models.settings import (
    validate_model_request_options,
)
from a3_research_agent.host.models.vision import (
    DIRECT_VISION_INSTRUCTIONS,
    MAX_FRAME_BYTES,
    MAX_TOTAL_BYTES,
    VisionBudgetError,
    VisionRequest,
    prepare_frame_content,
    prepare_vision_request,
    selected_frame_calls,
)
from a3_research_agent.host.telemetry.diagnostics import CURRENT_REQUEST, upstream_status
from a3_research_agent.token_count import count_tokens, estimate_request_tokens

from .instructions import MODEL_FIELDS, SUMMARY_INSTRUCTIONS
from .schemas import (
    SummaryRequest,
)


def _web_enabled(service) -> bool:
    """Report optional web availability without inventing it for a test double."""
    status = getattr(service, "status", None)
    if not callable(status):
        return False
    try:
        value = status()
    except (AttributeError, TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("enabled") is True


def model_input_headroom(window, output_tokens, output_reserve):
    return window * 9 // 10 - max(output_tokens, output_reserve)


def provider_rejected_images(error):
    status = upstream_status(error)
    if status in {413, 415}:
        return True
    if status not in {400, 422}:
        return False
    body = getattr(error, "body", None) or getattr(getattr(error, "response", None), "text", "")
    return bool(
        re.search(
            r"\b(input_image|image_url|images?|vision|multimodal)\b", str(body), re.IGNORECASE
        )
    )


def validation_request_shape(
    arguments: dict, image_count: int, estimated_tokens: int | None
) -> dict:
    inputs = arguments.get("input")
    return {
        "input_items": len(inputs) if isinstance(inputs, list) else None,
        "estimated_input_tokens": estimated_tokens,
        "tools": len(arguments.get("tools") or []),
        "forced_tool_choice": arguments.get("tool_choice") == "required",
        "max_output_tokens": arguments.get("max_output_tokens"),
        "images": image_count,
    }


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
        inject_task = bool(
            purpose == "decision"
            and (self.task_instructions or self.input_versions)
            and experiment == self.main_owner
        )
        role = (
            "summary"
            if purpose == "summary"
            else "worker"
            if purpose == "vision"
            or (
                self.main_owner is not None
                and experiment != self.main_owner
                and experiment not in self.root_owners
            )
            else "main"
        )
        route = self.fixed_roles.get(
            role, {"model_name": self.model_name, "request_options": self.fixed_model_options}
        )
        options = route["request_options"]
        arguments.update(validate_model_request_options(options))
        input_manifest, active_task = (
            self.input_versions.model_instructions()
            if self.input_versions is not None
            else ({}, self.task_instructions)
        )
        if inject_task:
            specific = arguments.get("instructions")
            if specific is not None and not isinstance(specific, str):
                raise HTTPException(422, "Instructions must be text")
            arguments["instructions"] = active_task + "\n\n" + specific if specific else active_task
        if isinstance(arguments.get("input"), str):
            arguments["input"] = [{"role": "user", "content": arguments["input"]}]
        selections = (
            selected_frame_calls(arguments.get("input"))
            if purpose == "decision" and role == "main"
            else []
        )
        feedback_ids = []
        if self.human_feedback is not None and purpose == "decision":
            try:
                arguments, feedback_ids = self.human_feedback.apply(experiment, arguments)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        if self.token_budget is None:
            raise HTTPException(503, "Model token budget is unavailable")
        output_tokens = arguments.get("max_output_tokens")
        if output_tokens is None:
            output_tokens = self.token_budget.output_reserve_tokens
        if type(output_tokens) is not int or output_tokens <= 0:
            raise HTTPException(422, "Output allowance must be a positive integer")
        model_maximum = self.token_budget.maximum_output_tokens(role)
        arguments["max_output_tokens"] = model_maximum or output_tokens
        if role == "summary" and model_maximum is not None:
            arguments["instructions"] = (
                arguments.get("instructions", "")
                + f" Aim for {output_tokens} tokens in the handoff and keep the required evidence."
            )
        estimated_tokens = None
        frames_rejected = False
        text_arguments = arguments
        if self.token_budget:
            estimate_arguments = arguments
            if purpose == "vision":
                # Use the resolution-based admission reserve for images.
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
            if role == "summary" and model_maximum is not None:
                limit = self.token_budget.summary_input_tokens
            elif role == "summary":
                output_limit = arguments["max_output_tokens"]
                summary_instructions = arguments.get("instructions", "")
                while True:
                    arguments["max_output_tokens"] = output_limit
                    arguments["instructions"] = (
                        summary_instructions
                        + f" Working-memory output budget: {output_limit} tokens. "
                        "Return a complete summary within this budget."
                    )
                    estimated_tokens = estimate_request_tokens(
                        arguments, self.token_budget.provider_reserve_tokens
                    )
                    available = window - estimated_tokens
                    if available <= 0:
                        raise HTTPException(422, "Summary input leaves no output space")
                    if output_limit <= available:
                        break
                    output_limit = available
                limit = window - max(self.token_budget.output_reserve_tokens, output_limit)
            else:
                limit = model_input_headroom(
                    window,
                    output_tokens if model_maximum is None else 0,
                    self.token_budget.output_reserve_tokens,
                )
            if estimated_tokens > limit:
                if purpose == "vision":
                    raise HTTPException(422, {"code": "vision_budget_exceeded"})
                raise HTTPException(422, "Estimated model input exceeds configured token headroom")
            if selections:
                text_arguments = arguments.copy()
                vision_frames, vision_image_reserve, estimated_tokens = self.attach_selected_frames(
                    arguments, experiment, selections, limit, estimated_tokens
                )
        started = time.monotonic()
        try:
            try:
                response = self.forward_model(model=route["model_name"], **arguments)
            except Exception as error:
                if not (selections and vision_frames and provider_rejected_images(error)):
                    raise
                self.diagnostics.model_validation_error(
                    error, validation_request_shape(arguments, len(vision_frames), estimated_tokens)
                )
                # The provider rejected the request before inference. Keep the completed
                # tool batch and tell the next decision to retry with smaller selections.
                arguments = text_arguments
                for selection in selections:
                    estimated_tokens = self.vision_notice(
                        arguments,
                        selection,
                        "could not attach the frames because the provider rejected the images. "
                        "Try a smaller group or use vision_inspect.",
                        limit,
                    )
                vision_frames, vision_image_reserve = [], 0
                frames_rejected = True
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
            status = upstream_status(error)
            if status == 400:
                self.diagnostics.model_validation_error(
                    error,
                    validation_request_shape(arguments, len(vision_frames or []), estimated_tokens),
                )
            if status is not None and 400 <= status < 500:
                headers = {"X-A3-Model-Error": "upstream_bad_request"} if status == 400 else None
                raise HTTPException(
                    status, "Model upstream request failed", headers=headers
                ) from None
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
        priced = pricing.cost(data.get("usage"))
        if priced is not None:
            entry["cost"] = priced
            entry["cost_currency"] = pricing.currency()
        if purpose == "summary":
            entry["summary_feedback_refs"] = summary_refs or []
        if purpose == "vision" or vision_frames:
            entry.update(
                model_purpose="vision" if purpose == "vision" else "decision",
                vision_frame_count=len(vision_frames or []),
                vision_frames=vision_frames or [],
                image_admission_reserve_tokens=vision_image_reserve,
            )
        if frames_rejected:
            entry["vision_provider_rejected"] = True
        if feedback_ids:
            entry["human_feedback_ids"] = feedback_ids
        if inject_task:
            entry["task_instructions_sha256"] = hashlib.sha256(active_task.encode()).hexdigest()
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
        inject_task = experiment == self.main_owner
        instructions = (
            self.input_versions.model_instructions()[1]
            if self.input_versions is not None
            else self.task_instructions
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
        injected = {"instructions": instructions if inject_task else "", "feedback": notes}
        main_route = (
            self.main_owner is None
            or experiment == self.main_owner
            or experiment in self.root_owners
        )
        window = (
            self.token_budget.main_context_tokens
            if main_route
            else self.token_budget.worker_context_tokens
        )
        injected_reserve = (
            count_tokens(injected)
            + self.token_budget.provider_reserve_tokens
            + 512
            + 16 * len(notes)
        )
        summary_maximum = self.token_budget.maximum_output_tokens("summary")
        return {
            "enabled": True,
            "summary_operation": "/v1/summary",
            "capabilities": {
                # Only advertise optional tool families the deployment can actually serve.
                "web": _web_enabled(self.web_service),
                "vision": True,
            },
            "unit": "estimated_tokens",
            "tokenizer": "o200k_base",
            "trigger_tokens": self.token_budget.trigger_tokens,
            "decision_input_tokens": window * 9 // 10 - self.token_budget.output_reserve_tokens,
            "target_tokens": self.token_budget.target_tokens,
            "summary_input_tokens": self.token_budget.summary_input_tokens,
            "injected_reserve_tokens": injected_reserve,
            "summary_reserve_tokens": count_tokens(SUMMARY_INSTRUCTIONS)
            + count_tokens(refs)
            + self.token_budget.provider_reserve_tokens
            + 256,
            "main_context_tokens": self.token_budget.main_context_tokens,
            "summary_context_tokens": self.token_budget.summary_context_tokens,
            **(
                {"summary_model_max_output_tokens": summary_maximum}
                if summary_maximum is not None
                else {}
            ),
            "feedback_refs": refs,
            "model_routes_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "main": self.model_name,
                        "main_options": self.fixed_model_options,
                        "roles": self.fixed_roles,
                        **(
                            {"summary_max_output_tokens": summary_maximum}
                            if summary_maximum is not None
                            else {}
                        ),
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
        if self.token_budget is None:
            raise HTTPException(503, "Summary token budget is unavailable")
        result = self.forward_response(
            {
                "instructions": SUMMARY_INSTRUCTIONS,
                "input": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "max_output_tokens": body.max_output_tokens or self.token_budget.target_tokens,
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
            "direct_tool": "vision_view",
            "max_image_bytes": 512 * 1024,
            "max_total_bytes": 3 * 1024 * 1024,
            "max_side_pixels": 1024,
            "model_role": "worker",
            "source": "authorized job workspace or outputs PNG/JPEG artifacts",
            "note": "Batch size depends on image bytes, context budget and provider limits",
        }

    def read_vision_frame(self, owner, job_id, path, *, area="workspace"):
        artifact = self.remote(
            "artifact",
            {
                "experiment_id": self.job_owner(owner, job_id, shared_read=True),
                "job_id": job_id,
                "path": path,
                "max_bytes": MAX_FRAME_BYTES,
                **({"area": area} if area == "outputs" else {}),
            },
        )
        if area == "outputs" and artifact.get("area") != "outputs":
            raise ValueError("Controller did not confirm the outputs area")
        return artifact

    def attach_selected_frames(self, arguments, owner, selections, limit, estimated_tokens):
        """Add original pixels to this decision with safe text fallbacks per selection."""
        text_view = {**arguments, "input": list(arguments["input"])}
        image_messages = {}
        reserve = byte_count = 0
        frames = []
        provider_reserve = self.token_budget.provider_reserve_tokens
        for selection in selections:
            prefix = f"vision_view ({selection['call_id']}): "
            try:
                if byte_count >= MAX_TOTAL_BYTES:
                    raise VisionBudgetError("Decision image byte budget exhausted")
                request = VisionRequest.model_validate(
                    tool_json.loads(selection.get("arguments", ""))
                )
                for frame in request.frames:
                    if "area" not in frame.model_fields_set:
                        frame.area = "outputs"
                content, metadata, image_tokens = prepare_frame_content(
                    request,
                    owner,
                    self.read_vision_frame,
                    input_limit=limit - estimated_tokens,
                    byte_limit=MAX_TOTAL_BYTES - byte_count,
                    lossless=True,
                )
                content.insert(0, {"type": "input_text", "text": prefix + "Selected frames"})
                caption = [part for part in content if part["type"] == "input_text"]
                text_message = {"role": "user", "content": caption}
                candidate = {**text_view, "input": [*text_view["input"], text_message]}
                if not frames:
                    candidate["instructions"] = (
                        arguments.get("instructions", "") + "\n\n" + DIRECT_VISION_INSTRUCTIONS
                    ).strip()
                next_estimate = (
                    estimate_request_tokens(candidate, provider_reserve) + reserve + image_tokens
                )
                if next_estimate > limit:
                    raise VisionBudgetError("Labeled images exceed available input")
            except VisionBudgetError:
                notice = "could not attach the frames within the image or context budget. Select smaller groups or resize."
            except (HTTPException, ValueError, TypeError):
                notice = "could not attach the selected frames. Check artifact access, paths, format and labels."
            else:
                image_messages[len(text_view["input"])] = {"role": "user", "content": content}
                text_view = candidate
                estimated_tokens = next_estimate
                reserve += image_tokens
                byte_count += sum(frame["request_bytes"] for frame in metadata)
                frames.extend({**frame, "call_id": selection["call_id"]} for frame in metadata)
                continue
            estimated_tokens = (
                self.vision_notice(text_view, selection, notice, limit - reserve) + reserve
            )
        arguments.update(text_view)
        for index, message in image_messages.items():
            arguments["input"][index] = message
        return frames, reserve, estimated_tokens

    def vision_notice(self, arguments, selection, notice, limit):
        """Use a short tool result if the regular notice cannot fit."""
        messages = list(arguments["input"])
        text = f"vision_view ({selection['call_id']}): {notice}"
        candidate = {**arguments, "input": [*messages, {"role": "user", "content": text}]}
        provider_reserve = self.token_budget.provider_reserve_tokens
        estimated = estimate_request_tokens(candidate, provider_reserve)
        if estimated <= limit:
            arguments.update(candidate)
            return estimated
        for index in range(len(messages) - 1, -1, -1):
            item = messages[index]
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == selection["call_id"]
            ):
                messages[index] = {**item, "output": '{"error":"frames_unavailable"}'}
                break
        candidate["input"] = messages
        estimated = estimate_request_tokens(candidate, provider_reserve)
        if estimated <= limit:
            arguments.update(candidate)
            return estimated
        return estimate_request_tokens(arguments, provider_reserve)

    def inspect_vision(self, body: dict, authorization: Annotated[str | None, Header()] = None):
        self.accepting_resources()
        actor = self.owner(authorization)
        if self.token_budget is None:
            raise HTTPException(503, "Model token budget is unavailable")
        input_limit = model_input_headroom(
            self.token_budget.worker_context_tokens,
            0,
            self.token_budget.output_reserve_tokens,
        )
        if not self.vision_slots.acquire(blocking=False):
            raise HTTPException(429, "Visual inspection busy; no model request was dispatched")
        try:
            try:
                arguments, metadata, reserve = prepare_vision_request(
                    body, actor, self.read_vision_frame, input_limit=input_limit
                )
            except VisionBudgetError:
                raise HTTPException(422, {"code": "vision_budget_exceeded"}) from None
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
            if result.get("status") != "completed" or not text.strip():
                raise HTTPException(
                    502,
                    "Visual response incomplete or missing; no observation confirmed",
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
