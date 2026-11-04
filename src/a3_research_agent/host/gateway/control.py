"""Control Gateway behavior on the shared runtime instance."""

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import Header, HTTPException, Query

from .schemas import (
    FeedbackPreviewRequest,
    FeedbackUpdateRequest,
    InputUpdateRequest,
)


class ControlRoutes:
    def require_operator(self, authorization):
        supplied = (authorization or "").removeprefix("Bearer ")
        if not self.operator_token or not secrets.compare_digest(supplied, self.operator_token):
            raise HTTPException(403, "Host operator authorization required")

    def prepare_inputs(self, source_dir: str | None = None):
        if self.input_versions is None:
            raise HTTPException(503, "Versioned Agent inputs are not configured")
        try:
            return (
                self.input_versions.prepare_directory(Path(source_dir))
                if source_dir is not None
                else self.input_versions.prepare()
            )
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from None

    def preview_inputs(
        self,
        authorization: Annotated[str | None, Header()] = None,
        source_dir: str | None = Query(default=None, min_length=1, max_length=4096),
    ):
        self.require_operator(authorization)
        proposal = self.prepare_inputs(source_dir)
        return {
            "current": self.input_versions.manifest,
            "proposed": {key: proposal[key] for key in ("digest", "files")},
            "warning": "热加载会更新本轮目标、验收和Prompt。已发出的请求仍使用原版本；后续请求使用新版。需确认预览内容摘要后执行。",
        }

    def reload_inputs(
        self, body: InputUpdateRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.require_operator(authorization)
        if self.input_versions is None:
            raise HTTPException(503, "Versioned Agent inputs are not configured")
        with self.input_versions.lock:
            self.accepting_resources()
            proposal = self.prepare_inputs(body.source_dir)
            if not secrets.compare_digest(body.confirm, proposal["digest"]):
                raise HTTPException(409, "Preview changed; inspect and confirm the current digest")
            try:
                return self.input_versions.activate(proposal, "hot_reload")
            except OSError:
                raise HTTPException(
                    503, "Agent input update failed; inspect Host storage"
                ) from None

    def prepare_feedback(self, text):
        self.accepting_resources()
        if self.human_feedback is None or self.evolution is None:
            raise HTTPException(503, "Human feedback and a main session must be configured")
        actor = self.evolution.main_owner
        if actor not in self.sessions.values():
            raise HTTPException(409, "The main session is no longer accepting model requests")
        try:
            return self.human_feedback.preview(text, actor)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    def preview_feedback(
        self, body: FeedbackPreviewRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.require_operator(authorization)
        with self.session_lock:
            return {
                **self.prepare_feedback(body.text),
                "warning": "确认后反馈将加入当前主 Agent 下一次完整历史模型请求；已发出的请求和正在运行的作业继续执行。此操作属于人工参与。",
            }

    def reload_feedback(
        self, body: FeedbackUpdateRequest, authorization: Annotated[str | None, Header()] = None
    ):
        self.require_operator(authorization)
        # Serialize confirmation with promotion so feedback cannot silently change recipients.
        with self.session_lock:
            proposal = self.prepare_feedback(body.text)
            if not secrets.compare_digest(body.confirm, proposal["digest"]):
                raise HTTPException(409, "Feedback content or main identity changed; preview again")
            try:
                registered = self.human_feedback.register_content(
                    body.text.encode("utf-8"), proposal["owner"]
                )
            except OSError:
                raise HTTPException(503, "Feedback update failed; inspect Host storage") from None
            return {**registered, "owner": proposal["owner"], "delivery": "next_model_request"}
