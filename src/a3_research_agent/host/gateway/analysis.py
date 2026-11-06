"""Analysis Gateway behavior on the shared runtime instance."""

from contextlib import nullcontext
from typing import Annotated, Literal

from fastapi import Header, HTTPException

from a3_research_agent.host.gateway.wait_signals import snapshot as wait_snapshot

from .schemas import AnalysisRequest, AnalysisResponseRequest, ObservationRequest


class AnalysisRoutes:
    def read_telemetry(
        self,
        authorization: Annotated[str | None, Header()] = None,
        scope: Literal["self", "descendants", "run"] = "self",
    ):
        with self.session_lock:
            actor = self.owner(authorization)
            allowed = (
                set(self.research_read_scopes(actor)).__contains__
                if scope == "run"
                else self.telemetry_scope(actor, scope == "descendants")
            )
            return self.telemetry.snapshot(allowed)

    def record_observations(
        self,
        body: ObservationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            actor = self.owner(authorization)
            self.telemetry.observe(actor, body.model_dump())
            self.save_checkpoint()
            return {"recorded": True}

    def analyzer_owner(self, authorization):
        actor = self.owner(authorization)
        current_main = self.evolution.main_owner if self.evolution else self.main_owner
        if current_main is None or actor != current_main:
            raise HTTPException(403, "Only the current main Agent can request analysis")
        return actor

    def wait_signals(self, authorization: Annotated[str | None, Header()] = None):
        self.accepting_resources()
        # Snapshot each source under its own lock; never nest source locks or
        # wait for a model. Updates racing this read remain visible next poll.
        with self.session_lock:
            actor = self.owner(authorization)
            current_main = self.evolution.main_owner if self.evolution else self.main_owner
        with self.input_versions.lock if self.input_versions else nullcontext():
            manifest = dict(self.input_versions.manifest) if self.input_versions else {}
        with self.human_feedback.lock if self.human_feedback else nullcontext():
            feedback = [dict(x) for x in self.human_feedback.records] if self.human_feedback else []
        with self.analyzer.lock:
            analysis = (
                [dict(x) for x in self.analyzer.state["records"]] if self.analyzer.enabled else []
            )
        return wait_snapshot(
            actor,
            is_main=actor == current_main,
            manifest=manifest,
            feedback=feedback,
            analysis=analysis,
        )

    def read_analysis(self, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            self.owner(authorization)
            return self.analyzer.latest(None)

    def read_analysis_report(
        self,
        analysis_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            self.owner(authorization)
            return self.analyzer.get(None, analysis_id)

    def request_analysis(
        self,
        body: AnalysisRequest | None = None,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            self.accepting_resources()
            actor = self.analyzer_owner(authorization)
            if body and body.enqueue_only:
                return self.analyzer.enqueue(body.trigger)
            snapshot = self.telemetry.snapshot(set(self.research_read_scopes(actor)).__contains__)
        from fastapi.responses import JSONResponse

        report = self.analyzer.analyze(actor, snapshot, body.trigger if body else "manual")
        return JSONResponse(report, status_code=202 if report["status"] == "running" else 200)

    def respond_to_analysis(
        self,
        body: AnalysisResponseRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            actor = self.analyzer_owner(authorization)
            return self.analyzer.respond(actor, body.analysis_id, body.decision, body.reason)
