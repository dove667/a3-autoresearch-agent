"""Compatibility facade for the componentized standard-library Controller."""

import os
import subprocess
import sys
import time

from a3_research_agent.execution.controller.artifacts import (
    read_artifact,
)
from a3_research_agent.execution.controller.docker import (
    cleanup_temporary_directory,
    container_state,
    create_container,
    docker,
)
from a3_research_agent.execution.controller.files import (
    ACTIVE_STATES,
    MAX_ARCHIVE_LOG_BYTES,
    MAX_FILE_BYTES,
    MAX_INPUT_BYTES,
    atomic_json,
    directory_storage_bytes,
    job_timeout,
    safe_path,
    validate_submit,
    write_files,
)
from a3_research_agent.execution.controller.lifecycle import (
    cancel,
    cancelled_before_start,
    capture_container_logs,
    durable_json,
    lifecycle_lock,
    owned_container,
    reclaim_terminal,
    state_for_status,
    status,
    watch,
)
from a3_research_agent.execution.controller.store import (
    JobStore,
)
from a3_research_agent.execution.controller.submission import (
    submit,
)
from a3_research_agent.execution.exports.operations import (
    export_operation,
)
from a3_research_agent.execution.exports.readonly import (
    EXPORT_ID,
    IDENTITY,
    ReadonlyExports,
    open_source,
    sync_directory,
    write_json,
)
from a3_research_agent.execution.scheduling.gpu import (
    GpuLease,
    acquire_gpu,
    active_jobs,
    container_gpu_memory,
    count_active_jobs,
    gpu_devices,
    gpu_process_memory,
    record_scheduler_event,
    select_shared_gpu,
)

from .cli import main, run

__all__ = [
    "ACTIVE_STATES",
    "EXPORT_ID",
    "IDENTITY",
    "MAX_ARCHIVE_LOG_BYTES",
    "MAX_FILE_BYTES",
    "MAX_INPUT_BYTES",
    "GpuLease",
    "JobStore",
    "ReadonlyExports",
    "acquire_gpu",
    "active_jobs",
    "atomic_json",
    "cancel",
    "cancelled_before_start",
    "capture_container_logs",
    "cleanup_temporary_directory",
    "container_gpu_memory",
    "container_state",
    "count_active_jobs",
    "create_container",
    "directory_storage_bytes",
    "docker",
    "durable_json",
    "export_operation",
    "gpu_devices",
    "gpu_process_memory",
    "job_timeout",
    "lifecycle_lock",
    "main",
    "open_source",
    "os",
    "owned_container",
    "read_artifact",
    "reclaim_terminal",
    "record_scheduler_event",
    "run",
    "safe_path",
    "select_shared_gpu",
    "state_for_status",
    "status",
    "submit",
    "subprocess",
    "sync_directory",
    "sys",
    "time",
    "validate_submit",
    "watch",
    "write_files",
    "write_json",
]
