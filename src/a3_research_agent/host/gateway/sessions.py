"""Start and observe the run's native research session."""

import logging
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Query

from a3_research_agent.host.runs.comparisons import check_comparison
from a3_research_agent.host.sessions.workspace import MAX_ARTIFACT_BYTES

from .schemas import ComparisonRequest, SessionStopRequest


class SessionRoutes:
    def start_local_harness(
        self,
        command: list[str],
        files: dict[str, str],
        authorization: str,
        read_only_exports: list[str] | None = None,
    ):
        """Start the fixed published Harness using the Host root identity."""
        with self.session_lock:
            self.accepting_resources()
            parent = self.owner(authorization)
            if parent not in self.root_owners:
                raise HTTPException(403, "Harness startup requires the Host root identity")
            if self.session_runtime is None:
                raise HTTPException(503, "Native execution is not configured")
            self.deployment_call(self.session_runtime.check_capacity)
            scope = "session-" + secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = scope
            granted = []
            try:
                identifiers = read_only_exports or []
                self.validate_exports(parent, identifiers)
                for identifier in identifiers:
                    self.remote(
                        "export-grant",
                        {
                            "experiment_id": parent,
                            "export_id": identifier,
                            "recipient": scope,
                        },
                    )
                    granted.append(identifier)
                result = self.deployment_call(
                    self.session_runtime.create,
                    scope,
                    command,
                    files,
                    token,
                    inputs=self.initial_inputs,
                )
            except Exception:
                self.sessions.pop(token, None)
                for identifier in granted:
                    try:
                        self.remote(
                            "export-revoke",
                            {
                                "experiment_id": parent,
                                "export_id": identifier,
                                "recipient": scope,
                            },
                        )
                    except HTTPException:
                        logging.getLogger(__name__).error("Export grant rollback requires retry")
                raise
            self.session_records[scope] = {
                "parent": parent,
                "owner": scope,
                "token": token,
                "closed": False,
            }
            self.main_owner = scope
            self.session_event("created", scope)
            return result

    def list_sessions(
        self,
        authorization: Annotated[str | None, Header()] = None,
        include_status: bool = False,
    ):
        self.owner(authorization)
        with self.session_lock:
            result = []
            for candidate, record in self.session_records.items():
                item = {
                    "session_id": candidate,
                    "parent": record["parent"],
                    "closed": record["closed"],
                }
                if include_status:
                    state = (
                        self.deployment_call(
                            self.session_runtime.status, record["owner"], candidate
                        )
                        if not record["closed"]
                        else {"status": "closed"}
                    )
                    item.update(status=state.get("status"), exit_code=state.get("exit_code"))
                result.append(item)
            return {"sessions": result}

    def comparison(
        self, body: ComparisonRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.owner(authorization)
        return check_comparison(body.baseline, body.candidate)

    def self_status(self, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            record = self.own_session(authorization)
            actor = record["owner"]
            return {
                **self.deployment_call(self.session_runtime.status, actor, actor),
                "is_main": actor == self.main_owner,
            }

    def stop_self(
        self, body: SessionStopRequest, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            record = self.own_session(authorization)
            session_id = record["owner"]
            # This explicit request is terminal. It never decides acceptance or cancels a branch.
            self.session_event("self_stop_requested", session_id)
            result = self.deployment_call(self.session_runtime.stop, session_id, session_id)
            record["self_stopped"] = True
            record["stop_reason"] = body.reason
            self.sessions.pop(record["token"], None)
            self.session_event("self_stopped", session_id)
            return result

    def session_status(
        self, session_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        record = self.readable_session(authorization, session_id)
        return self.deployment_call(self.session_runtime.status, record["owner"], session_id)

    def cancel_session(
        self,
        session_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            self.managed_session(authorization, session_id)
            return self.deployment_call(self.close_session, session_id)

    def session_artifact(
        self,
        session_id: str,
        path: str,
        authorization: Annotated[str | None, Header()] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=MAX_ARTIFACT_BYTES)] = None,
        text: bool = False,
    ):
        record = self.readable_session(authorization, session_id)
        if text:
            if limit is not None and limit > 8000:
                raise HTTPException(422, "Text pages are at most 8000 characters")
            return self.deployment_call(
                self.session_runtime.artifact_text,
                record["owner"],
                session_id,
                path,
                offset,
                limit or 4000,
            )
        if limit is not None or offset:
            return self.deployment_call(
                self.session_runtime.artifact, record["owner"], session_id, path, offset, limit
            )
        return self.deployment_call(
            self.session_runtime.artifact, record["owner"], session_id, path
        )

    def create_fixture(self, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            self.accepting_resources()
            experiment = self.owner(authorization)
            if self.fixtures is None:
                raise HTTPException(503, "Isolation fixtures are not configured")
            result = self.deployment_call(self.fixtures.create, experiment)
            self.save_checkpoint()
            return result

    def verify_fixture(
        self, fixture_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        return self.deployment_call(self.fixtures.verify, self.owner(authorization), fixture_id)

    def cancel_fixture(
        self, fixture_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            result = self.deployment_call(
                self.fixtures.cancel, self.owner(authorization), fixture_id
            )
            self.save_checkpoint()
            return result
