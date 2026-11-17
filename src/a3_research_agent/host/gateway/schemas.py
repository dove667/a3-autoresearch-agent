"""Strict Gateway request contracts shared by route components."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .evaluation import EvaluationComparison


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_calls: int = Field(ge=0, le=1000000000, strict=True)
    repeated_reads: int = Field(ge=0, le=1000000000, strict=True)
    retrieved_characters: int = Field(ge=0, le=1000000000, strict=True)
    delivered_reads: int = Field(ge=0, le=1000000000, strict=True)


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript: str = Field(min_length=1)
    source: str = Field(pattern="^context-history/snapshot-[0-9a-f]{64}\\.json$")
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)


class WorkspaceFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")


class JobWorkspaceFileReference(WorkspaceFileReference):
    destination: str | None = Field(default=None, min_length=1, max_length=4096)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    command: list[str] = Field(min_length=1, max_length=256)
    gpu: StrictBool = False
    retry_on_gpu_contention: StrictBool = True
    gpu_memory_mb: int | None = Field(default=None, ge=1, strict=True)
    files: dict[str, str] = Field(default_factory=dict)
    input_exports: list[str] = Field(default_factory=list, max_length=64)
    evaluation: EvaluationComparison | None = None
    workspace_files: list[JobWorkspaceFileReference] = Field(default_factory=list, max_length=16)


class JobWaitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[str] = Field(min_length=1, max_length=64)
    seconds: int = Field(default=25, ge=0, le=300, strict=True)
    cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    location: Literal["outputs", "workspace"] = "outputs"


class ExportGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)


class SessionStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class InputUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(pattern="^[0-9a-f]{64}$")


class FeedbackPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=65536)


class FeedbackUpdateRequest(FeedbackPreviewRequest):
    confirm: str = Field(pattern="^[0-9a-f]{64}$")


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: dict
    candidate: dict
