"""Standard-library JSON Controller CLI and background-process modes."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from a3_research_agent.execution.exports.operations import export_operation
from a3_research_agent.execution.exports.readonly import ReadonlyExports

from .artifacts import read_artifact
from .docker import docker
from .files import MAX_INPUT_BYTES
from .lifecycle import cancel, status, watch
from .store import JobStore
from .submission import submit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", default="")
    parser.add_argument("--backend", choices=["docker", "process"], default="docker")
    parser.add_argument("--managed-jobs", action="store_true")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--process-python", type=Path)
    parser.add_argument("--a3-root", type=Path)
    parser.add_argument("--host-memory-reserve-mb", type=int, default=32768)
    parser.add_argument("--host-job-memory-mb", type=int, default=8192)
    parser.add_argument("--host-cpu-reserve", type=float, default=2)
    parser.add_argument("--host-job-cpus", type=float, default=2)
    parser.add_argument("--disk-reserve-bytes", type=int, default=68719476736)
    parser.add_argument("--cpus", type=float, default=16)
    parser.add_argument("--memory", default="64g")
    parser.add_argument("--pids-limit", type=int, default=1024)
    parser.add_argument("--tmp-root", type=Path)
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--gpu-ids", default="")
    parser.add_argument("--share-gpus", action="store_true")
    parser.add_argument("--max-gpus", type=int, default=4)
    parser.add_argument("--gpu-memory-mb", type=int, default=512)
    parser.add_argument("--gpu-headroom-mb", type=int, default=2048)
    parser.add_argument("--gpu-capacity-mb", type=int, default=0)
    parser.add_argument("--run-storage-bytes", type=int, default=0)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--preserve-models-path", action="store_true")
    parser.add_argument("--model-cache-root", type=Path)
    parser.add_argument("--datasets-root", type=Path)
    parser.add_argument(
        "operation",
        choices=[
            "submit",
            "jobs-list",
            "jobs-status",
            "gpu-snapshot",
            "status",
            "cancel",
            "artifact",
            "health",
            "usage",
            "export-create",
            "export-status",
            "export-manifest",
            "exports-list",
            "exports-check",
            "export-grant",
            "export-revoke",
            "export-delete",
        ],
    )
    args = parser.parse_args()
    if args.managed_jobs and args.backend != "process":
        parser.error("Managed jobs require the process backend")
    if args.max_jobs < 0 or args.run_storage_bytes < 0 or args.timeout < 0:
        parser.error("max-jobs, storage bytes and timeout must be nonnegative")
    if (
        args.max_gpus < 1
        or args.gpu_memory_mb < 1
        or args.gpu_headroom_mb < 0
        or args.gpu_capacity_mb < 0
    ):
        parser.error("GPU count/reservation must be positive; headroom/capacity nonnegative")
    if args.backend == "process" and (not args.data_root or not args.process_python):
        parser.error("Process backend requires data-root and process-python")
    if (
        min(args.host_memory_reserve_mb, args.disk_reserve_bytes, args.host_cpu_reserve) < 0
        or min(args.host_job_memory_mb, args.host_job_cpus) <= 0
    ):
        parser.error("Host reserves must be nonnegative and admission estimates positive")
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        if args.operation == "gpu-snapshot":
            from a3_research_agent.execution.scheduling.gpu import gpu_devices

            devices = gpu_devices()
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "observed_at": time.time(),
                        "gpus": [
                            {
                                "index": device["index"],
                                "total_mb": device["total_mb"],
                                "used_mb": device["used_mb"],
                                "free_mb": device["total_mb"] - device["used_mb"],
                                "utilization": device["utilization"],
                            }
                            for device in devices
                        ],
                    }
                )
            )
            return
        if args.backend == "process":
            from .processes import validate_data_paths

            validate_data_paths(
                args.data_root, [args.root, args.tmp_root or args.root / "temporary"]
            )
        store = JobStore(args.root)
        if args.operation == "jobs-list":
            print(json.dumps(store.list_jobs(request)))
            return
        if args.operation == "jobs-status":
            print(json.dumps(store.job_statuses(request)))
            return
        if args.operation.startswith("export"):
            print(json.dumps(export_operation(store, request, args)))
            return
        if args.operation == "usage":
            print(json.dumps(store.update_host_storage(request)))
            return
        if args.operation == "health":
            if args.backend == "process":
                from a3_research_agent.execution.scheduling import host_resources

                from .processes import validate_data_paths

                validate_data_paths(
                    args.data_root,
                    [
                        store.root,
                        args.process_python.resolve(strict=True),
                        args.tmp_root or store.root / "temporary",
                    ],
                )
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "protocol_version": 1,
                            "backend": "process",
                            "python": str(args.process_python),
                            "data_root": str(args.data_root),
                            "host_resources": host_resources.sample(args.data_root),
                            "limits": {
                                "cpus": None,
                                "memory": None,
                                "host_memory_reserve_mb": args.host_memory_reserve_mb,
                                "disk_reserve_bytes": args.disk_reserve_bytes,
                                "run_storage_bytes": args.run_storage_bytes,
                            },
                        }
                    )
                )
                return
            image = docker(["image", "inspect", "--format", "{{.Id}}", args.image]).stdout.strip()
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "protocol_version": 1,
                        "image": args.image,
                        "image_id": image,
                        "limits": {
                            "cpus": args.cpus,
                            "memory": args.memory,
                            "pids_limit": args.pids_limit,
                            "max_jobs": args.max_jobs,
                            "timeout_seconds": args.timeout,
                            "run_storage_bytes": args.run_storage_bytes,
                            "gpu_mode": "memory_shared" if args.share_gpus else "exclusive",
                            "max_gpus": args.max_gpus,
                            "gpu_memory_mb": args.gpu_memory_mb,
                            "gpu_headroom_mb": args.gpu_headroom_mb,
                            "gpu_capacity_mb": args.gpu_capacity_mb,
                        },
                        "assets": {
                            "models": "/models" if args.models_root else None,
                            "datasets": "/datasets" if args.datasets_root else None,
                            "model_cache": "/model-cache" if args.model_cache_root else None,
                        },
                    }
                )
            )
            return
        handlers = {"status": status, "cancel": cancel, "artifact": read_artifact}
        if args.operation == "submit" and args.managed_jobs:
            from .managed_queue import enqueue

            response = enqueue(store, request, args)
        else:
            response = (
                submit(store, request, args)
                if args.operation == "submit"
                else handlers[args.operation](store, request)
            )
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        response = {"status": "error", "error": type(error).__name__, "message": str(error)}
    print(json.dumps(response))


def run() -> None:
    if "--dispatch" in sys.argv:
        from .managed_queue import run_dispatcher

        dispatcher = argparse.ArgumentParser()
        dispatcher.add_argument("--dispatch", action="store_true")
        dispatcher.add_argument("--root", required=True, type=Path)
        run_dispatcher(dispatcher.parse_args().root)
    elif "--seal-export" in sys.argv:
        sealer = argparse.ArgumentParser()
        sealer.add_argument("--seal-export", action="store_true")
        sealer.add_argument("--root", type=Path, required=True)
        sealer.add_argument("--export-id", required=True)
        sealer.add_argument("--storage-limit", type=int, default=0)
        args = sealer.parse_args()
        store = JobStore(args.root)
        exports = ReadonlyExports(args.root)
        record = exports.load(args.export_id)
        exports.set_pid(args.export_id, os.getpid())
        exports.seal(
            args.export_id, lambda: store.run_storage_bytes(record["run_id"]), args.storage_limit
        )
    elif "--watch" in sys.argv:
        watcher = argparse.ArgumentParser()
        watcher.add_argument("--watch", action="store_true")
        watcher.add_argument("--root", type=Path, required=True)
        watcher.add_argument("--job-id", required=True)
        watcher.add_argument("--timeout", type=int, required=True)
        watcher.add_argument("--lock-fd", type=int)
        watch(watcher.parse_args())
    else:
        main()
