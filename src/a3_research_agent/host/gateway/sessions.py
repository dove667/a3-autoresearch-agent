"""Sessions Gateway behavior on the shared runtime instance."""

import logging
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Query

from a3_research_agent.host.runs.comparisons import check_comparison
from a3_research_agent.host.sessions.containers import MAX_ARTIFACT_BYTES

from .schemas import (
    ComparisonRequest,
    SessionRequest,
    SessionStopRequest,
    TrialDecision,
    TrialRequest,
    VersionRequest,
)


class SessionRoutes:
    def create_session(
        self, body: SessionRequest, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            self.accepting_resources()
            self.owner(authorization)
            raise HTTPException(
                403, "Research runs have no workers; use one linear evolution trial"
            )

    def _create_session(self, body: SessionRequest, authorization):
        """Create only Host-selected initial sessions or the pending linear successor."""
        if self.sandbox is None:
            raise HTTPException(503, "Candidate execution is not configured")
        with self.session_lock:
            self.accepting_resources()
            parent = self.owner(authorization)
            scope = "candidate-" + secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = scope
            granted = []
            try:
                selected_files, selected_parent, version_id = (
                    body.files,
                    body.parent_version,
                    body.version_id,
                )
                assembly = None
                if body.workspace_files:
                    if not body.version_id or body.files or body.parent_version:
                        raise HTTPException(
                            422, "workspace_files requires version_id without files/parent_version"
                        )
                    self.own_session(authorization)
                    if self.versions is None:
                        raise HTTPException(503, "Candidate versions are not configured")
                    base = self.deployment_call(self.versions.owned, parent, body.version_id)
                    attached, receipt = self.deployment_call(
                        self.sandbox.read_workspace_files,
                        parent,
                        [x.model_dump() for x in body.workspace_files],
                    )
                    selected_files = self.deployment_call(
                        self.versions.assembled_files, parent, body.version_id, attached
                    )
                    selected_parent, version_id = body.version_id, None
                    assembly = {
                        "base_version_id": body.version_id,
                        "base_source_sha256": base["source_sha256"],
                        "workspace_files": receipt,
                    }
                if body.read_only_exports:
                    if self.evolution is None:
                        raise HTTPException(403, "Only the current main Agent can share exports")
                    self.deployment_call(self.evolution.require_main, parent)
                    self.validate_exports(parent, body.read_only_exports)
                    for identifier in body.read_only_exports:
                        self.remote(
                            "export-grant",
                            {"experiment_id": parent, "export_id": identifier, "recipient": scope},
                        )
                        granted.append(identifier)
                if (
                    not body.workspace_files
                    and body.version_id
                    and (body.files or body.parent_version)
                ):
                    raise HTTPException(422, "Choose version_id or files with parent_version")
                if self.versions is not None:
                    if hasattr(self.sandbox, "check_capacity"):
                        self.deployment_call(self.sandbox.check_capacity)
                    if version_id is None:
                        previous = selected_parent or self.children.get(parent, {}).get(
                            "version_id"
                        )
                        version_id = self.deployment_call(
                            self.versions.create, parent, selected_files, previous
                        )["version_id"]
                    self.deployment_call(self.versions.grant, parent, scope, version_id)
                    if (
                        self.initial_inputs is not None
                        and parent in self.root_owners
                        and not self.children
                    ):
                        result = self.deployment_call(
                            self.sandbox.create_initial,
                            scope,
                            body.command,
                            token,
                            version_id,
                            self.initial_inputs,
                        )
                    else:
                        result = self.deployment_call(
                            self.sandbox.create_version, scope, body.command, token, version_id
                        )
                elif version_id:
                    raise HTTPException(503, "Candidate versions are not configured")
                else:
                    result = self.deployment_call(
                        self.sandbox.create, scope, body.command, body.files, token
                    )
            except Exception:
                self.sessions.pop(token, None)
                for identifier in granted:
                    try:
                        self.remote(
                            "export-revoke",
                            {"experiment_id": parent, "export_id": identifier, "recipient": scope},
                        )
                    except HTTPException:
                        logging.getLogger(__name__).error("Export grant rollback requires retry")
                raise
            if assembly is not None:
                assembly["derived_version_id"] = version_id
                assembly["derived_source_sha256"] = self.deployment_call(
                    self.versions.owned, parent, version_id
                )["source_sha256"]
                result = {**result, "assembly": assembly}
            self.children[result["session_id"]] = {
                "parent": parent,
                "owner": scope,
                "token": token,
                "closed": False,
                "version_id": result.get("version_id"),
                "purpose": body.purpose,
            }
            if assembly is not None:
                self.children[result["session_id"]]["assembly"] = assembly
            self.scope_parents[scope] = parent
            if self.evolution and self.start_main_session and len(self.children) == 1:
                self.evolution.main_owner = scope
                self.evolution.save()
            self.session_event("created", result["session_id"])
            return result

    def start_local_harness(
        self,
        command: list[str],
        files: dict[str, str],
        authorization: str,
        read_only_exports: list[str] | None = None,
    ):
        """Import a local package inside the Host; no unbounded HTTP route is exposed."""
        with self.session_lock:
            self.accepting_resources()
            parent = self.owner(authorization)
            if parent not in self.root_owners:
                raise HTTPException(403, "Local Harness imports require the Host root identity")
            if self.versions is None or self.sandbox is None:
                raise HTTPException(503, "Candidate execution is not configured")
            request = SessionRequest(command=command)
            # Local retained manifests can select more assets than one bounded HTTP request.
            # Every reference is still checked before any candidate is started.
            request.read_only_exports = read_only_exports or []
            if hasattr(self.sandbox, "check_capacity"):
                self.deployment_call(self.sandbox.check_capacity)
            version = self.versions.create_local(parent, files)
            request.version_id = version["version_id"]
            return self._create_session(request, authorization)

    def evolution_state(self):
        if self.evolution is None or self.versions is None:
            raise HTTPException(503, "Linear evolution is not configured")
        return self.evolution

    def inspect_evolution(self, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        with self.session_lock:
            state = self.evolution_state()
            if (
                actor not in self.root_owners
                and actor != state.main_owner
                and actor not in state.trials
            ):
                raise HTTPException(403, "Evolution state is not accessible")
            if actor in state.trials and actor != state.main_owner:
                return {"main_owner": state.main_owner, "trial": state.trials[actor]}
            return state.snapshot()

    def start_trial(
        self, body: TrialRequest, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            if getattr(self.sandbox, "published_source", None) is not None:
                self.owner(authorization)
                raise HTTPException(403, "Published Harness changes require a maintainer release")
            state = self.evolution_state()
            actor = self.owner(authorization)
            self.deployment_call(state.require_main, actor)
            if body.workspace_files:
                raise HTTPException(
                    422, "Freeze an assembled version before starting a formal trial"
                )
            if state.pending():
                raise HTTPException(409, "Decide the current candidate before starting another")
            self.deployment_call(self.versions.owned, actor, body.baseline_version)
            request = SessionRequest(**body.model_dump(exclude={"baseline_version", "checklist"}))
            result = self._create_session(request, authorization)
            state.register(
                result["session_id"], body.baseline_version, result["version_id"], body.checklist
            )
            self.save_checkpoint()
            return {**result, "role": "candidate_main"}

    def decide_trial(
        self,
        session_id: str,
        body: TrialDecision,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            state = self.evolution_state()
            actor = self.owner(authorization)
            self.deployment_call(state.require_main, actor)
            trial = state.trials.get(session_id)
            if not trial or trial["status"] != "testing":
                raise HTTPException(409, "No pending trial with this identity")
            record = self.child(authorization, session_id)
            # The Agent judges the checklist. Host only verifies evidence accessibility.
            if body.retain:
                if not body.evidence:
                    raise HTTPException(422, "Retaining a candidate requires evidence paths")
                status = self.deployment_call(self.sandbox.status, record["owner"], session_id)
                if status.get("status") != "running":
                    raise HTTPException(409, "Only a running candidate can take over")
                for path in body.evidence:
                    self.deployment_call(self.sandbox.artifact, record["owner"], session_id, path)
            else:
                self.deployment_call(self.close_child, session_id)
            result = state.decide(actor, session_id, body.retain, body.reason, body.evidence)
            self.save_checkpoint()
            return result

    def list_sessions(
        self,
        authorization: Annotated[str | None, Header()] = None,
        include_status: bool = False,
    ):
        self.owner(authorization)
        with self.session_lock:
            result = []
            for candidate, record in self.children.items():
                item = {
                    "session_id": candidate,
                    "parent": record["parent"],
                    "closed": record["closed"],
                    "version_id": record.get("version_id"),
                    "purpose": record.get("purpose", "successor"),
                }
                if include_status:
                    state = (
                        self.deployment_call(self.sandbox.status, record["owner"], candidate)
                        if not record["closed"]
                        else {"status": "closed"}
                    )
                    item.update(status=state.get("status"), exit_code=state.get("exit_code"))
                result.append(item)
            return {"sessions": result}

    def save_version(
        self, body: VersionRequest, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            self.accepting_resources()
            experiment = self.owner(authorization)
            if self.versions is None:
                raise HTTPException(503, "Candidate versions are not configured")
            return self.deployment_call(
                self.versions.create, experiment, body.files, body.parent_version
            )

    def list_versions(self, authorization: Annotated[str | None, Header()] = None):
        experiment = self.owner(authorization)
        if self.versions is None:
            raise HTTPException(503, "Candidate versions are not configured")
        with self.session_lock:
            versions = {}
            for scope in self.research_read_scopes(experiment):
                for version in self.versions.list(scope):
                    self.versions.grant(scope, experiment, version["version_id"])
                    versions[version["version_id"]] = version
            return {"versions": list(versions.values())}

    def comparison(
        self, body: ComparisonRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.owner(authorization)
        return check_comparison(body.baseline, body.candidate)

    def self_status(self, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            record = self.own_session(authorization)
            actor = record["owner"]
            current = self.evolution.main_owner if self.evolution else self.main_owner
            previous_main = actor == current or bool(
                self.evolution
                and any(
                    item.get("from") == actor or item.get("to") == actor
                    for item in self.evolution.transitions
                )
            )
            return {
                **self.deployment_call(self.sandbox.status, actor, actor),
                "is_main": actor == current,
                "has_been_main": previous_main,
            }

    def stop_self(
        self, body: SessionStopRequest, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            record = self.own_session(authorization)
            session_id = record["owner"]
            # This explicit request is terminal. It never decides acceptance or cancels a branch.
            self.session_event("self_stop_requested", session_id)
            result = self.deployment_call(self.sandbox.stop, session_id, session_id)
            record["self_stopped"] = True
            record["stop_reason"] = body.reason
            self.sessions.pop(record["token"], None)
            self.session_event("self_stopped", session_id)
            return result

    def session_status(
        self, session_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        record = self.readable_session(authorization, session_id)
        return self.deployment_call(self.sandbox.status, record["owner"], session_id)

    def cancel_session(
        self,
        session_id: str,
        cascade: bool = False,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            self.child(authorization, session_id)
            open_descendants = [
                candidate
                for candidate in self.descendants(session_id)
                if candidate != session_id and not self.children[candidate]["closed"]
            ]
            if open_descendants and not cascade:
                raise HTTPException(
                    409, {"reason": "descendants_open", "sessions": open_descendants}
                )
            return self.deployment_call(self.close_child, session_id)

    def session_artifact(
        self,
        session_id: str,
        path: str,
        authorization: Annotated[str | None, Header()] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=MAX_ARTIFACT_BYTES)] = None,
    ):
        record = self.readable_session(authorization, session_id)
        if limit is not None or offset:
            return self.deployment_call(
                self.sandbox.artifact, record["owner"], session_id, path, offset, limit
            )
        return self.deployment_call(self.sandbox.artifact, record["owner"], session_id, path)

    def descendant_versions(
        self, session_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        record = self.readable_session(authorization, session_id)
        if self.versions is None:
            raise HTTPException(503, "Candidate versions are not configured")
        experiment = self.owner(authorization)
        with self.session_lock:
            saved = self.versions.list(record["owner"])
            for version in saved:
                self.versions.grant(record["owner"], experiment, version["version_id"])
            return {"versions": saved}

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
