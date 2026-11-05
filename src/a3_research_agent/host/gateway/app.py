"""Construct one Gateway runtime and bind its domain routes."""

import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI

from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.models.context import ContextBudget
from a3_research_agent.host.models.settings import (
    validate_model_request_options,
    validate_role_models,
)
from a3_research_agent.host.runs.evolution import EvolutionState
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.telemetry.analysis import TrajectoryAnalyzer
from a3_research_agent.host.telemetry.diagnostics import GatewayDiagnostics
from a3_research_agent.host.telemetry.trajectory import TrajectoryTelemetry
from a3_research_agent.host.web.gateway import WebConfig, WebGateway

from .analysis import AnalysisRoutes
from .control import ControlRoutes
from .jobs import JobRoutes
from .models import ModelRoutes
from .schemas import ObservationRequest
from .sessions import SessionRoutes
from .state import GatewayState
from .web import WebRoutes


class Gateway(
    GatewayState, SessionRoutes, AnalysisRoutes, WebRoutes, ControlRoutes, ModelRoutes, JobRoutes
):
    def __init__(
        self,
        *,
        model_name: str,
        sessions: dict[str, str],
        forward_model: Callable,
        remote_call: Callable,
        usage_log: Path,
        sandbox=None,
        fixtures=None,
        versions=None,
        initial_inputs: Path | None = None,
        main_owner: str | None = None,
        start_main_session: bool = False,
        phase_instructions: str = "",
        state_store: RunStateStore | None = None,
        restored_state: dict | None = None,
        input_versions: InputVersions | None = None,
        operator_token: str | None = None,
        resource_monitor: Callable | None = None,
        human_feedback=None,
        web_gateway=None,
        model_request_options: dict | None = None,
        role_models: dict | None = None,
        context_budget: dict | None = None,
    ) -> None:
        self.model_name = model_name
        self.sessions = sessions
        self.forward_model = forward_model
        self.remote_call = remote_call
        self.usage_log = usage_log
        self.sandbox = sandbox
        self.fixtures = fixtures
        self.versions = versions
        self.initial_inputs = initial_inputs
        self.main_owner = main_owner
        self.start_main_session = start_main_session
        self.phase_instructions = phase_instructions
        self.state_store = state_store
        self.restored_state = restored_state
        self.input_versions = input_versions
        self.operator_token = operator_token
        self.resource_monitor = resource_monitor
        self.human_feedback = human_feedback
        self.web_gateway = web_gateway
        self.model_request_options = model_request_options
        self.role_models = role_models
        self.context_budget = context_budget
        self.fixed_model_options = validate_model_request_options(self.model_request_options)
        self.fixed_roles = validate_role_models(self.role_models)
        self.token_budget = ContextBudget(
            **(
                self.context_budget
                or {
                    "main_context_tokens": 500000,
                    "worker_context_tokens": 500000,
                    "summary_context_tokens": 500000,
                }
            )
        )
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=self.lifespan)
        self.telemetry = TrajectoryTelemetry()
        self.analyzer = TrajectoryAnalyzer(
            self.forward_model,
            self.fixed_roles.get(
                "worker",
                {"model_name": self.model_name, "request_options": self.fixed_model_options},
            ),
            self.state_store,
        )
        self.diagnostics = GatewayDiagnostics(
            self.usage_log.with_name("gateway-events.jsonl"), self.telemetry.record
        )
        self.diagnostics.attach(self.app)
        self.log_lock = threading.Lock()
        self.session_lock = threading.RLock()
        self.children: dict[str, dict] = {}
        self.scope_parents: dict[str, str] = {}
        self.jobs: dict[str, set[str]] = {}
        self.root_owners = set(self.sessions.values())
        self.app.state.closing = False
        self.evolution = (
            EvolutionState(self.usage_log.with_name("evolution.json"), self.main_owner)
            if self.main_owner
            else None
        )
        if self.restored_state is not None:
            self.sessions.clear()
            self.sessions.update(self.restored_state["sessions"])
            self.root_owners = set(self.restored_state["root_owners"])
            self.children.update(self.restored_state["children"])
            runtime_records = getattr(self.sandbox, "records", {})
            for session_id, record in self.children.items():
                if record.get("self_stopped") or runtime_records.get(session_id, {}).get(
                    "self_stopped"
                ):
                    record["self_stopped"] = True
                    self.sessions.pop(record["token"], None)
            self.scope_parents.update(self.restored_state["scope_parents"])
            self.jobs.update(
                {key: set(value) for key, value in self.restored_state["jobs"].items()}
            )
            if self.evolution and self.restored_state.get("evolution"):
                saved = self.restored_state["evolution"]
                self.evolution.main_owner = saved["main_owner"]
                self.evolution.trials = saved["trials"]
                self.evolution.transitions = saved["transitions"]
            if self.fixtures is not None:
                self.fixtures.records = self.restored_state.get("fixtures", {})
            for actor, values in self.restored_state.get("telemetry_observations", {}).items():
                self.telemetry.observe(
                    actor, ObservationRequest.model_validate(values).model_dump()
                )
        self.app.state.save_checkpoint = self.save_checkpoint
        self.app.state.result_location = self.result_location
        self.app.state.begin_shutdown = self.begin_shutdown
        self.app.state.close_resources = self.close_resources
        self.web_service = self.web_gateway or WebGateway(
            self.usage_log.parent / ".host" / "web", WebConfig()
        )
        self.app.state.start_local_harness = self.start_local_harness
        self.vision_slots = threading.BoundedSemaphore(2)
        self.app.add_api_route("/telemetry", self.read_telemetry, methods=["GET"])
        self.app.add_api_route(
            "/telemetry/observations", self.record_observations, methods=["POST"]
        )
        self.app.add_api_route("/wait/signals", self.wait_signals, methods=["GET"])
        self.app.add_api_route("/analysis", self.read_analysis, methods=["GET"])
        self.app.add_api_route(
            "/analysis/{analysis_id}", self.read_analysis_report, methods=["GET"]
        )
        self.app.add_api_route("/analysis", self.request_analysis, methods=["POST"])
        self.app.add_api_route("/analysis/response", self.respond_to_analysis, methods=["POST"])
        self.app.add_api_route("/web/status", self.web_status, methods=["GET"])
        self.app.add_api_route("/web/search", self.web_search, methods=["POST"])
        self.app.add_api_route("/web/fetch", self.web_fetch, methods=["POST"])
        self.app.add_api_route("/sessions", self.create_session, methods=["POST"])
        self.app.add_api_route("/evolution", self.inspect_evolution, methods=["GET"])
        self.app.add_api_route("/evolution/trials", self.start_trial, methods=["POST"])
        self.app.add_api_route(
            "/evolution/trials/{session_id}/decision", self.decide_trial, methods=["POST"]
        )
        self.app.add_api_route("/sessions", self.list_sessions, methods=["GET"])
        self.app.add_api_route("/versions", self.save_version, methods=["POST"])
        self.app.add_api_route("/versions", self.list_versions, methods=["GET"])
        self.app.add_api_route("/checks/comparison", self.comparison, methods=["POST"])
        self.app.add_api_route("/sessions/self", self.self_status, methods=["GET"])
        self.app.add_api_route("/sessions/self/stop", self.stop_self, methods=["POST"])
        self.app.add_api_route("/sessions/{session_id}", self.session_status, methods=["GET"])
        self.app.add_api_route("/sessions/{session_id}", self.cancel_session, methods=["DELETE"])
        self.app.add_api_route(
            "/sessions/{session_id}/artifacts", self.session_artifact, methods=["GET"]
        )
        self.app.add_api_route(
            "/sessions/{session_id}/versions", self.descendant_versions, methods=["GET"]
        )
        self.app.add_api_route("/fixtures/isolation", self.create_fixture, methods=["POST"])
        self.app.add_api_route(
            "/fixtures/isolation/{fixture_id}", self.verify_fixture, methods=["GET"]
        )
        self.app.add_api_route(
            "/fixtures/isolation/{fixture_id}", self.cancel_fixture, methods=["DELETE"]
        )
        self.app.add_api_route("/health", self.health, methods=["GET"])
        self.app.add_api_route("/control/inputs", self.preview_inputs, methods=["GET"])
        self.app.add_api_route("/control/inputs", self.reload_inputs, methods=["POST"])
        self.app.add_api_route("/control/feedback/preview", self.preview_feedback, methods=["POST"])
        self.app.add_api_route("/control/feedback", self.reload_feedback, methods=["POST"])
        self.app.add_api_route("/context/config", self.context_configuration, methods=["GET"])
        self.app.add_api_route("/v1/responses", self.responses, methods=["POST"])
        self.app.add_api_route("/v1/summary", self.summarize, methods=["POST"])
        self.app.add_api_route("/vision/status", self.vision_status, methods=["GET"])
        self.app.add_api_route("/vision/inspect", self.inspect_vision, methods=["POST"])
        self.app.add_api_route("/jobs", self.submit, methods=["POST"])
        self.app.add_api_route("/jobs/exports", self.list_exports, methods=["GET"])
        self.app.add_api_route("/jobs/{job_id}/exports", self.publish_export, methods=["POST"])
        self.app.add_api_route("/jobs/exports/{export_id}", self.export_status, methods=["GET"])
        self.app.add_api_route(
            "/jobs/exports/{export_id}/manifest", self.export_manifest, methods=["GET"]
        )
        self.app.add_api_route(
            "/jobs/exports/{export_id}/grants", self.grant_export, methods=["POST"]
        )
        self.app.add_api_route(
            "/jobs/exports/{export_id}/grants/{session_id}", self.revoke_export, methods=["DELETE"]
        )
        self.app.add_api_route("/jobs/exports/{export_id}", self.delete_export, methods=["DELETE"])
        self.app.add_api_route("/jobs/{job_id}", self.status, methods=["GET"])
        self.app.add_api_route("/jobs/{job_id}", self.cancel, methods=["DELETE"])
        self.app.add_api_route("/jobs/{job_id}/artifacts", self.artifact, methods=["GET"])


def create_app(
    *,
    model_name: str,
    sessions: dict[str, str],
    forward_model: Callable,
    remote_call: Callable,
    usage_log: Path,
    sandbox=None,
    fixtures=None,
    versions=None,
    initial_inputs: Path | None = None,
    main_owner: str | None = None,
    start_main_session: bool = False,
    phase_instructions: str = "",
    state_store: RunStateStore | None = None,
    restored_state: dict | None = None,
    input_versions: InputVersions | None = None,
    operator_token: str | None = None,
    resource_monitor: Callable | None = None,
    human_feedback=None,
    web_gateway=None,
    model_request_options: dict | None = None,
    role_models: dict | None = None,
    context_budget: dict | None = None,
) -> FastAPI:
    return Gateway(
        model_name=model_name,
        sessions=sessions,
        forward_model=forward_model,
        remote_call=remote_call,
        usage_log=usage_log,
        sandbox=sandbox,
        fixtures=fixtures,
        versions=versions,
        initial_inputs=initial_inputs,
        main_owner=main_owner,
        start_main_session=start_main_session,
        phase_instructions=phase_instructions,
        state_store=state_store,
        restored_state=restored_state,
        input_versions=input_versions,
        operator_token=operator_token,
        resource_monitor=resource_monitor,
        human_feedback=human_feedback,
        web_gateway=web_gateway,
        model_request_options=model_request_options,
        role_models=role_models,
        context_budget=context_budget,
    ).app


# Preserve public request types from the original Gateway module.
from .schemas import (
    AnalysisRequest,
    AnalysisResponseRequest,
    ComparisonRequest,
    ExportGrantRequest,
    ExportRequest,
    FeedbackPreviewRequest,
    FeedbackUpdateRequest,
    InputUpdateRequest,
    JobRequest,
    SessionRequest,
    SessionStopRequest,
    SummaryRequest,
    TrialDecision,
    TrialRequest,
    VersionRequest,
    WorkspaceFileReference,
)

__all__ = [
    "AnalysisRequest",
    "AnalysisResponseRequest",
    "ComparisonRequest",
    "ExportGrantRequest",
    "ExportRequest",
    "FeedbackPreviewRequest",
    "FeedbackUpdateRequest",
    "Gateway",
    "InputUpdateRequest",
    "JobRequest",
    "ObservationRequest",
    "SessionRequest",
    "SessionStopRequest",
    "SummaryRequest",
    "TrialDecision",
    "TrialRequest",
    "VersionRequest",
    "WorkspaceFileReference",
    "create_app",
]
