"""Scoped telemetry and wait notifications on the shared Gateway."""

from contextlib import nullcontext
from typing import Annotated, Literal

from fastapi import Header

from a3_research_agent.host.gateway.wait_signals import snapshot as wait_snapshot

from .schemas import ObservationRequest


class TelemetryRoutes:
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
        return wait_snapshot(
            actor, is_main=actor == current_main, manifest=manifest, feedback=feedback
        )
