"""Strict Gateway request contracts shared by route components."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: Literal["manual", "repetition", "no_progress", "before_handoff", "periodic"] = "manual"
    enqueue_only: bool = False


class AnalysisResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(pattern="^[0-9a-f]{32}$")
    decision: Literal["adopt", "reject", "defer"]
    reason: str = Field(min_length=1, max_length=500)


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_calls: int = Field(ge=0, le=1000000000, strict=True)
    repeated_reads: int = Field(ge=0, le=1000000000, strict=True)
    retrieved_characters: int = Field(ge=0, le=1000000000, strict=True)
    delivered_reads: int = Field(ge=0, le=1000000000, strict=True)
    open_items: int = Field(ge=0, le=1000000000, strict=True)
    waiting_items: int = Field(ge=0, le=1000000000, strict=True)
    closed_items: int = Field(ge=0, le=1000000000, strict=True)


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compact_retry: StrictBool = False
    previous: str = Field(max_length=6000)
    fragment: str = Field(min_length=1, max_length=500000)
    source: str = Field(pattern="^context-history/snapshot-[0-9a-f]{64}\\.json$")


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    gpu: StrictBool = False
    gpu_memory_mb: int | None = Field(default=None, ge=1, strict=True)
    files: dict[str, str] = Field(default_factory=dict)
    input_exports: list[str] = Field(default_factory=list, max_length=64)


class WorkspaceFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    files: dict[str, str] = Field(default_factory=dict)
    version_id: str | None = None
    parent_version: str | None = None
    read_only_exports: list[str] = Field(default_factory=list, max_length=64)
    purpose: Literal["worker", "candidate_validation"] = "worker"
    workspace_files: list[WorkspaceFileReference] = Field(default_factory=list, max_length=16)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    location: Literal["outputs", "workspace"] = "outputs"


class ExportGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)


class TrialRequest(SessionRequest):
    baseline_version: str
    checklist: str = Field(min_length=1, max_length=32000)


class TrialDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retain: StrictBool
    reason: str = Field(min_length=1, max_length=16000)
    evidence: list[str] = Field(default_factory=list, max_length=128)


class SessionStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class InputUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(pattern="^[0-9a-f]{64}$")
    source_dir: str | None = Field(default=None, min_length=1, max_length=4096)


class FeedbackPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=65536)


class FeedbackUpdateRequest(FeedbackPreviewRequest):
    confirm: str = Field(pattern="^[0-9a-f]{64}$")


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str]
    parent_version: str | None = None


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: dict
    candidate: dict
