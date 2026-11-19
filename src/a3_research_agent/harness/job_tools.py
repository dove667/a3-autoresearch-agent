"""Translate common job operations into the existing owner-checked Host API."""

import hashlib
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from . import benchmark_policy, benchmark_runner, tool_results
from .job_outputs import ARTIFACT_FIELDS, read_batch
from .job_preparation import PREFLIGHT_SCHEMA, admission_rejection, normalize_preflight
from .tool_contracts import (
    ToolInputError,
    arguments,
    boolean,
    definition,
    fail,
    integer,
    relative_name,
    text,
)

STRING = {"type": "string"}
JOB_ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
PREPARED = re.compile(r"prepared-[a-f0-9]{32}")
ATTACHMENTS = {
    "type": "array",
    "maxItems": 16,
    "items": {
        "type": "object",
        "properties": {"path": STRING, "destination": STRING},
        "required": ["path"],
        "additionalProperties": False,
    },
}
REQUEST_FIELDS = {
    "name": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120,
        "description": "Short human-readable job name shown in the research dashboard.",
    },
    "command": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 256},
    "gpu": {"type": "boolean"},
    "attachments": ATTACHMENTS,
    "input_exports": {"type": "array", "items": STRING, "maxItems": 64},
    "gpu_memory_mb": {"type": "integer", "minimum": 1},
    "retry_on_gpu_contention": {"type": "boolean"},
    "preflight": PREFLIGHT_SCHEMA,
    "evaluation": {
        "type": "object",
        "properties": {
            "baseline_job_id": JOB_ID,
            "baseline_manifest": STRING,
            "candidate_manifest": STRING,
        },
        "required": ["baseline_job_id", "baseline_manifest", "candidate_manifest"],
        "additionalProperties": False,
    },
}
MAX_BATCH = 16
PREPARED_ID = {"type": "string", "pattern": "^prepared-[a-f0-9]{32}$"}
BENCHMARK_FIELDS = {
    "checkpoint",
    "task",
    "profile",
    "episodes",
    "seed_start",
    "seeds",
    "policy",
    "max_steps",
    "n_action_steps",
    "source_column",
    "tilt_level",
    "box_quota",
    "diagnostics",
    "name",
}
DATASET_FIELDS = {
    "dataset_root": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1024,
        "description": "Remote absolute dataset directory, for example /data1/.../ds-merged.",
    },
    "repo_id": {"type": "string", "maxLength": 200},
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
}
API_FIELDS = {
    "symbols": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 80},
        "maxItems": 16,
        "description": "Function names to locate; omit for the core set.",
    },
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
}
BATCH_FIELDS = {
    "jobs": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_BATCH,
        "items": {"type": "object", "properties": REQUEST_FIELDS, "required": ["command"]},
    },
    "prepared_ids": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_BATCH,
        "items": PREPARED_ID,
    },
}
TOOLS = [
    definition(
        "job_submit",
        "Submit work: one prepared GPU job (prepared_id), one CPU command, or a batch. "
        "prepared_ids submits up to 16 saved GPU requests at once; jobs submits up to 16 CPU "
        "requests at once with per-item results. For a GPU job first call job_prepare, wait for "
        "its preflight_job_id, then submit its prepared_id; never repeat the full GPU request. "
        "attachments references workspace text files (scripts and experiment configs); hashes are "
        "automatic. Give each job a short human-readable name; the dashboard uses it as the "
        "primary job label. Use input_exports for data and weights. Reconcile submission_unknown "
        "by job_id before retrying.",
        {
            **REQUEST_FIELDS,
            "prepared_id": PREPARED_ID,
            **BATCH_FIELDS,
        },
    ),
    definition(
        "job_prepare",
        "Save GPU requests and run their real CPU preflights. Supply the production command, "
        "attachments and assets once, for one request or up to 16 in jobs. Each entry must handle "
        "A3_RESEARCH_PREFLIGHT=1, load real inputs, perform a minimal forward/data read, write "
        "a3-preflight.json with schema=1,status=passed, and exit. Give each request a short "
        "human-readable name. Returns prepared_id and preflight_job_id per item. Wait for those "
        "jobs, then job_submit(prepared_ids=[...]).",
        {**REQUEST_FIELDS, **BATCH_FIELDS},
    ),
    definition(
        "benchmark",
        "Run one A3 evaluation cell through the fixed benchmark launcher. The tool writes "
        "benchmarks/a3_benchmark_runner.py, attaches it as benchmark_runner.py, chooses the "
        "task budget (2x5=500, 2x10=3200, scenarios=1000 except two-boxes with "
        "box_quota=10 uses 3200), and returns prepared_id, preflight_job_id, report "
        "and max_steps. Set diagnostics=true to record synchronized camera videos and a trace "
        "for each seed. The result includes outputs-relative diagnostic paths. Do not hand-build "
        "benchmark commands. Pass max_steps only to raise "
        "the budget above the task standard; a lower value is rejected. The report path is "
        "relative to the job outputs area. For an explicit seed list, pass seeds instead of "
        "episodes/seed_start, then use benchmark_group with the returned group_id. "
        "It advances preflights and aggregates per-seed results.",
        {
            "checkpoint": {
                "type": "string",
                "description": "Remote absolute checkpoint directory; the value after act:/smolvla:.",
            },
            "task": {"type": "string", "enum": list(benchmark_policy.TASK_STEPS)},
            "profile": {
                "type": "string",
                "enum": ["basic", "medium", "advanced"],
            },
            "episodes": {"type": "integer", "minimum": 1, "maximum": 20},
            "seed_start": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            "seeds": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "minItems": 1,
                "maxItems": 20,
                "uniqueItems": True,
            },
            "policy": {"type": "string", "enum": ["act", "smolvla"]},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 2147483647},
            "source_column": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": "Source column override. Omit for extension scenarios.",
            },
            "tilt_level": {
                "type": "string",
                "enum": ["mild", "fallen"],
                "description": "Tilted task only. Defaults to mild.",
            },
            "box_quota": {
                "type": "integer",
                "enum": [5, 10],
                "description": "Two-boxes task only. Cookies per target box, default 5.",
            },
            "diagnostics": {
                "type": "boolean",
                "description": "Save front and wrist camera videos and the per-step trace.",
            },
            "n_action_steps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Action horizon per replan; defaults to 25, the measured best setting.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "Optional job label; defaults to <task>-<profile>-<episodes>ep.",
            },
        },
        ["checkpoint", "task"],
    ),
    definition(
        "benchmark_group",
        "Advance an explicit-seed benchmark group and collect completed results. Schedules "
        "shards whose preflights passed, lets the Controller assign devices, and returns "
        "per-seed outcomes plus partial or complete totals. Pass timeout_seconds to wait "
        "for an event if nothing changed. Repeat with the same group_id after a notification.",
        {
            "group_id": {"type": "string", "pattern": "^benchmark-[0-9a-f]{32}$"},
            "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 300},
        },
        ["group_id"],
    ),
    definition(
        "dataset_audit",
        "Describe a training dataset before spending GPU time on it. Runs the deployed A3 "
        "auditor on a CPU job and adds the breakdown the auditor omits: episodes per task "
        "layout (2x5 or 2x10), per task instruction, success labels, seeds, frames and stored "
        "action mode, plus warnings for incomplete demonstrations, a single layout, or a "
        "count that disagrees with meta/info.json. Returns job_id and report (outputs-relative "
        "audit.json). Run it before designing a mixed-training step.",
        DATASET_FIELDS,
        ["dataset_root"],
    ),
    definition(
        "a3_api",
        "Report the real callable surface of the deployed A3 package: the file, line and "
        "signature of the core functions (audit_training_dataset, train_act, "
        "merge_cookie_datasets, aggregate_datasets, count_fill, collect_episodes) and the "
        "flags with their defaults for the collection and benchmark CLIs. Run it before "
        "writing a script that imports these; it reads installed source, so it cannot fail on "
        "an import. Returns job_id and report (outputs-relative api.json).",
        API_FIELDS,
    ),
    definition(
        "job_status",
        "Read a job's real status and bounded log tail by ID.",
        {"job_id": JOB_ID},
        ["job_id"],
    ),
    definition(
        "job_list",
        "List current or authorized historical jobs; after is a pagination cursor.",
        {
            "scope": {"type": "string", "description": "Use self or an authorized history scope."},
            "after": STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    definition(
        "job_artifact",
        "Read bounded UTF-8 pages from job artifacts, with source hashes. "
        "Use path for one file, paths (up to 16) for a batch, or directory for a folder listing "
        "(empty string lists the area root). Use area=outputs "
        "for persistent results and workspace for submitted source. Binary artifacts "
        "return type and hash metadata. offset/limit count characters per file.",
        ARTIFACT_FIELDS,
        ["job_id", "area"],
    ),
    definition(
        "job_cancel",
        "Cancel an owned queued or running job by ID; preserve its files.",
        {"job_id": JOB_ID},
        ["job_id"],
    ),
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        fail(
            "invalid_job_id",
            "Use the exact 32-character job ID returned by job_submit or job_list.",
        )
    return value


def attachment_references(references, workspace):
    if not isinstance(references, list) or len(references) > 16:
        fail(
            "invalid_attachments",
            "Attach at most 16 workspace text files using path and optional destination.",
        )
    result, total, names = [], 0, set()
    root = workspace.resolve()
    for reference in references:
        arguments(reference, {"path", "destination"})
        original = text(reference.get("path"), "attachment_path")
        if Path(original).is_absolute():
            try:
                original = Path(original).relative_to(root).as_posix()
            except ValueError:
                fail("invalid_attachment_path", "Select a file inside the current workspace.")
        original = relative_name(original, "attachment_path")
        source = root
        for part in PurePosixPath(original).parts:
            source /= part
            if source.is_symlink():
                fail(
                    "invalid_attachment_path",
                    "Attach regular files in workspace; symbolic links are not accepted.",
                )
        if not source.is_relative_to(root) or not source.is_file():
            fail(
                "attachment_missing",
                "Select a regular file inside workspace; use list or search_files first.",
            )
        destination = relative_name(reference.get("destination", original), "destination")
        if destination in names or any(
            PurePosixPath(destination) in PurePosixPath(name).parents
            or PurePosixPath(name) in PurePosixPath(destination).parents
            for name in names
        ):
            fail(
                "conflicting_attachments",
                "Use distinct remote destinations without file/directory conflicts.",
            )
        names.add(destination)
        if source.stat().st_size > 1024 * 1024:
            fail(
                "attachment_too_large",
                "Each text attachment must fit 1 MiB; use exports for large assets.",
            )
        raw = source.read_bytes()
        if len(raw) > 1024 * 1024:
            fail("attachment_too_large", "Each text attachment must fit 1 MiB.")
        try:
            raw.decode("utf-8")
        except UnicodeError:
            fail(
                "binary_attachment",
                "Attach UTF-8 text; reuse data, video and weights through exports.",
            )
        total += len(raw)
        if total > 8 * 1024 * 1024:
            fail("attachments_too_large", "Text attachments must total at most 8 MiB.")
        result.append(
            {
                "path": original,
                "sha256": hashlib.sha256(raw).hexdigest(),
                **({"destination": destination} if destination != original else {}),
            }
        )
    return result


A3_SOURCE_ROOT = posixpath.normpath(
    os.environ.get("A3_REMOTE_A3_ROOT") or "/data1/zsh/a3_dual_arm_sim"
)
WRITE_FLAGS = ("--output", "--output-dir", "--diagnostic-dir", "--root")


def remote_destinations(references):
    result = set()
    for reference in references:
        destination = reference.get("destination") or reference["path"]
        result.add(posixpath.normpath(destination))
    return result


def check_scripts(command, references):
    """A relative script must actually land in the job workspace.

    The real failure is `cd "$OUT"; python run_bench.py`: attachments are copied to
    the job workspace, not to an outputs directory the command changes into.
    """
    values = benchmark_policy.tokens(command)
    if "-m" in values:
        return
    destinations = remote_destinations(references)
    changed_directory = False
    for value in values:
        if value in {"cd", "pushd"}:
            changed_directory = True
            continue
        candidate = value.split("=", 1)[1] if value.startswith("--") and "=" in value else value
        candidate = candidate.rstrip(";|&()")
        if candidate.startswith("-") or not candidate.endswith(".py"):
            continue
        if candidate.startswith(("/", "$", "~")):
            continue
        script = posixpath.normpath(candidate)
        if changed_directory:
            fail(
                "script_path_outside_job_workspace",
                f"{script!r} is relative, but the command changes directory first. "
                "Attachments are copied to the job workspace, so this resolves elsewhere "
                "(the real failure: can't open file .../outputs/run_bench.py). Run it as "
                '"$A3_WORKSPACE/<name>" from inside a shell, or run it before changing directory.',
            )
        if script not in destinations:
            available = ", ".join(sorted(destinations)) or "none"
            fail(
                "script_not_attached",
                f"{script!r} is a relative script path, but no attachment places it in the "
                "job workspace (attached destinations: " + available + "). Attach the file "
                "with this destination, or run an installed module with python -m. Otherwise "
                "the job fails with can't open file.",
            )


def check_output_paths(command):
    """Keep evaluation outputs out of the shared A3 source checkout.

    The A3 benchmark CLI resolves a relative --diagnostic-dir against the source
    checkout, not the job cwd (the fixed dc0c5ee4 diagnostic directory), and an
    absolute path under the checkout writes into shared, non-job state.
    """
    values = benchmark_policy.tokens(command)
    for index, value in enumerate(values):
        if value in WRITE_FLAGS:
            flag = value
            target = values[index + 1] if index + 1 < len(values) else None
        elif value.startswith(tuple(flag + "=" for flag in WRITE_FLAGS)):
            flag, target = value.split("=", 1)
        else:
            continue
        if not target or target.startswith("-"):
            continue
        if flag == "--diagnostic-dir" and not target.startswith(("/", "$", "~")):
            fail(
                "output_outside_job_area",
                f"--diagnostic-dir {target!r} is relative. The A3 benchmark CLI resolves it "
                f"against {A3_SOURCE_ROOT}, not the job workspace, so it writes into the shared "
                "source checkout and a second run collides. Pass an absolute path under "
                "$A3_OUTPUTS (inside a shell command), for example "
                '"$A3_OUTPUTS/diagnostics-<seed>".',
            )
        if not target.startswith("/"):
            continue
        path = posixpath.normpath(target)
        if path == A3_SOURCE_ROOT or path.startswith(A3_SOURCE_ROOT + "/"):
            fail(
                "output_outside_job_area",
                f"{flag} writes to {target} under the shared A3 source checkout. Evaluation "
                "outputs must land in this job's outputs area: pass an absolute path under "
                "$A3_OUTPUTS (inside a shell command) or let the benchmark tool choose it.",
            )


def install_runner(workspace, module="benchmark_runner.py", name="a3_benchmark_runner.py"):
    """Write a maintained launcher into the workspace; every call overwrites it."""
    root = Path(workspace).resolve()
    directory = root / "benchmarks"
    target = directory / name
    relative = f"benchmarks/{name}"
    if directory.is_symlink() or target.is_symlink():
        fail(
            "invalid_benchmark_path",
            "Replace the benchmarks entry with a regular directory and file.",
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(root):
            fail("invalid_benchmark_path", f"Keep {relative} in workspace.")
        source = Path(__file__).with_name(module)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        fail(
            "invalid_benchmark_path",
            "Replace any file named benchmarks with a regular directory and retry.",
        )
    return relative


def benchmark(args, request, workspace, *, group_id=None):
    """Prepare one task/profile/seed cell with the fixed runner and its budget."""
    arguments(args, BENCHMARK_FIELDS)
    if "seeds" in args:
        from .benchmark_groups import prepare_group

        return prepare_group(args, request, workspace, benchmark)
    checkpoint = text(args.get("checkpoint"), "checkpoint", 2048).strip()
    if not checkpoint.startswith("/") or "\n" in checkpoint or "\r" in checkpoint:
        fail(
            "invalid_checkpoint",
            "Pass the remote absolute checkpoint directory (the value after act: or smolvla:), "
            "for example /data/.../checkpoints/020000/pretrained_model.",
        )
    task = args.get("task")
    if task not in benchmark_policy.MAX_SCORE:
        fail(
            "invalid_task",
            "Use 2x5, 2x10, or one of the six extension scenarios "
            "(missing-columns, partial-gaps, cross-column, tilted, prefilled, two-boxes).",
        )
    profile = args.get("profile", "basic")
    if profile not in {"basic", "medium", "advanced"}:
        fail("invalid_profile", "Use basic, medium or advanced.")
    episodes = integer(args.get("episodes", 5), "episodes", 1, 20)
    seed_start = integer(args.get("seed_start", 10000), "seed_start", 0, 2**31 - 1)
    policy = args.get("policy", "act")
    if policy not in {"act", "smolvla"}:
        fail("invalid_policy", "Use act or smolvla; the checkpoint is passed after the colon.")
    max_steps = args.get("max_steps")
    if max_steps is not None:
        max_steps = integer(max_steps, "max_steps", 1, 2**31 - 1)
    box_quota = args.get("box_quota")
    if box_quota is not None and (
        type(box_quota) is not int or box_quota not in {5, 10} or task != "two-boxes"
    ):
        fail("invalid_box_quota", "Pass box_quota 5 or 10 with task two-boxes.")
    budget_values = ["--box-quota", str(box_quota)] if box_quota is not None else []
    steps = benchmark_policy.budget(
        task, max_steps, benchmark_policy.steps_for(task, budget_values)
    )
    horizon = integer(args.get("n_action_steps", 25), "n_action_steps", 1, 50)
    name = args.get("name")
    if name is not None:
        name = text(name, "name", 120).strip()
    name = name or f"{task}-{profile}-{episodes}ep"
    tilt_level = args.get("tilt_level")
    if tilt_level is not None and (tilt_level not in {"mild", "fallen"} or task != "tilted"):
        fail("invalid_tilt_level", "Pass tilt_level mild or fallen with task tilted.")
    source_column = args.get("source_column")
    if source_column is not None and source_column not in {1, 2, 3, 4}:
        fail("invalid_source_column", "Pass source_column 1-4, or omit it for scenarios.")
    diagnostics = boolean(args.get("diagnostics", False), "diagnostics")
    install_runner(workspace)
    digest = hashlib.sha256(
        json.dumps(
            [
                task,
                profile,
                episodes,
                seed_start,
                policy,
                checkpoint,
                steps,
                horizon,
                tilt_level,
                box_quota,
                *(
                    [source_column, name, "diagnostics"]
                    if diagnostics
                    else [source_column]
                    if source_column
                    else []
                ),
                *([group_id] if group_id else []),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:8]
    output_dir = f"{task}-{profile}-{episodes}ep-seed{seed_start}-{digest}"
    command = [
        "python",
        "benchmark_runner.py",
        "--task",
        task,
        "--policy",
        f"{policy}:{checkpoint}",
        "--profile",
        profile,
        "--episodes",
        str(episodes),
        "--seed-start",
        str(seed_start),
        "--max-steps",
        str(steps),
        "--n-action-steps",
        str(horizon),
        *(["--source-column", str(source_column)] if source_column else []),
        *(["--tilt-level", tilt_level] if tilt_level else []),
        *(["--box-quota", str(box_quota)] if box_quota else []),
        *(["--diagnostics"] if diagnostics else []),
        "--output-dir",
        output_dir,
    ]
    job = submit_request(
        {
            "command": command,
            "attachments": [
                {
                    "path": "benchmarks/a3_benchmark_runner.py",
                    "destination": "benchmark_runner.py",
                }
            ],
            "gpu": True,
            "name": name,
        },
        Path(workspace).resolve(),
        default_gpu=True,
    )
    from . import job_preparation

    receipt = f"{output_dir}/seed-{seed_start}.receipt.json"
    job["benchmark_receipt"] = receipt
    prepared = job_preparation.prepare(job, request)
    return {
        **prepared,
        "report": f"{output_dir}/seed-{seed_start}.json",
        "receipt": receipt,
        "max_steps": steps,
        **(
            {"diagnostics": diagnostic_paths(output_dir, seed_start, episodes)}
            if diagnostics
            else {}
        ),
    }


def diagnostic_paths(output_dir, seed_start, episodes):
    paths = []
    directory = PurePosixPath(output_dir, "diagnostics")
    for seed in range(seed_start, seed_start + episodes):
        trace, videos = benchmark_runner.diagnostic_artifacts(directory, seed)
        paths.append(
            {
                "seed": seed,
                "trace": trace.as_posix(),
                "videos": {camera: path.as_posix() for camera, path in videos.items()},
            }
        )
    return paths


def dataset_audit(args, request, workspace):
    """Submit the maintained dataset auditor and say where its report lands."""
    arguments(args, DATASET_FIELDS)
    root = text(args.get("dataset_root"), "dataset_root", 1024).strip()
    if not root.startswith("/") or any(char in root for char in "\n\r"):
        fail(
            "invalid_dataset_root",
            "Pass an absolute dataset directory under runtime_paths.remote_datasets.",
        )
    repo = args.get("repo_id")
    if repo is not None:
        repo = text(repo, "repo_id", 200).strip()
    digest = hashlib.sha256(json.dumps([root, repo], sort_keys=True).encode()).hexdigest()[:8]
    output_dir = "dataset-audit-" + digest
    path = install_runner(workspace, "dataset_audit_runner.py", "a3_dataset_audit_runner.py")
    command = [
        "python",
        "a3_dataset_audit_runner.py",
        "--dataset-root",
        root,
        "--output-dir",
        output_dir,
    ]
    if repo:
        command += ["--repo-id", repo]
    job = submit_request(
        {
            "command": command,
            "attachments": [{"path": path, "destination": "a3_dataset_audit_runner.py"}],
            "gpu": False,
            "name": args.get("name") or "dataset-audit-" + digest,
        },
        Path(workspace).resolve(),
    )
    return {
        **submission_result(request("POST", "/jobs", job)),
        "report": f"{output_dir}/audit.json",
        "hint": "Wait for this job, then read report from the outputs area.",
    }


def a3_api(args, request, workspace):
    """Submit the surface reader and say where its report lands."""
    arguments(args, API_FIELDS)
    symbols = args.get("symbols")
    if symbols is not None and (
        not isinstance(symbols, list)
        or not 1 <= len(symbols) <= 16
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in symbols)
    ):
        fail("invalid_symbols", "Pass up to 16 function names, or omit symbols entirely.")
    digest = hashlib.sha256(json.dumps(symbols or [], sort_keys=True).encode()).hexdigest()[:8]
    output_dir = "a3-api-" + digest
    path = install_runner(workspace, "a3_api_runner.py", "a3_api_reader.py")
    command = ["python", "a3_api_reader.py", "--output-dir", output_dir]
    if symbols:
        command += ["--symbols", ",".join(item.strip() for item in symbols)]
    job = submit_request(
        {
            "command": command,
            "attachments": [{"path": path, "destination": "a3_api_reader.py"}],
            "gpu": False,
            "name": args.get("name") or "a3-api-" + digest,
        },
        Path(workspace).resolve(),
    )
    return {
        **submission_result(request("POST", "/jobs", job)),
        "report": f"{output_dir}/api.json",
        "hint": "Wait for this job, then read report from the outputs area.",
    }


def submit_request(args, workspace, *, default_gpu=False):
    arguments(args, REQUEST_FIELDS)
    command = args.get("command")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 256
        or any(not isinstance(part, str) or "\0" in part for part in command)
        or not command[0].strip()
    ):
        fail(
            "invalid_command", 'Use an argument array, for example ["python", "scripts/train.py"].'
        )
    body = {key: value for key, value in args.items() if key != "attachments"}
    if "name" in body:
        name = body["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            fail("invalid_job_name", "Use a nonempty job name of at most 120 characters.")
        body["name"] = name.strip()
    body["gpu"] = boolean(args.get("gpu", default_gpu), "gpu")
    if "preflight" in body:
        if not body["gpu"]:
            fail("invalid_preflight", "Preflight contracts apply to prepared GPU jobs.")
        try:
            body["preflight"] = normalize_preflight(body["preflight"])
        except ValueError as error:
            fail("invalid_preflight", str(error))
    references = attachment_references(args.get("attachments", []), workspace)
    check_scripts(command, references)
    check_output_paths(command)
    benchmark_policy.check_command(command)
    if references:
        body["workspace_files"] = references
    if "gpu_memory_mb" in body:
        integer(body["gpu_memory_mb"], "gpu_memory_mb", 1, 2**31 - 1)
    if "retry_on_gpu_contention" in body:
        boolean(body["retry_on_gpu_contention"], "retry_on_gpu_contention")
    exports = body.get("input_exports", [])
    if (
        not isinstance(exports, list)
        or len(exports) > 64
        or any(
            not isinstance(value, str) or not re.fullmatch(r"export-[a-f0-9]{32}", value)
            for value in exports
        )
    ):
        fail("invalid_input_exports", "Use at most 64 exact export IDs returned by export_create.")
    evaluation = body.get("evaluation")
    if evaluation is not None:
        arguments(evaluation, {"baseline_job_id", "baseline_manifest", "candidate_manifest"})
        identifier(evaluation.get("baseline_job_id"))
        for key in ("baseline_manifest", "candidate_manifest"):
            if not relative_name(evaluation.get(key), key).endswith(".json"):
                fail(
                    "invalid_evaluation_manifest",
                    "Select relative JSON manifests from job outputs.",
                )
    return body


GPU_NEEDS_PREPARATION = (
    "This is a GPU job, so it needs a preparation step first. Send the same request to "
    "job_prepare(jobs=[{...}]) (or job_prepare(command, attachments,...)) — it runs the CPU "
    'preflight and returns prepared_id. Then call job_submit(prepared_id="<that id>").'
)
BATCH_HINT = (
    "Independent commands can go in one call: job_submit(jobs=[{...}, {...}]) submits them "
    "together and returns one result per job."
)


def submission_result(result):
    result = admission_rejection(result)
    if isinstance(result, dict) and result.get("ok") and isinstance(result.get("job_id"), str):
        # One call per independent command multiplies latency; say the cheaper route once.
        return {**result, "batch_hint": BATCH_HINT}
    if not isinstance(result, dict) or result.get("http_error") != 502:
        return result
    try:
        detail = json.loads(result.get("body", ""))["detail"]
        job_id = identifier(detail["job_id"])
    except (ValueError, TypeError, KeyError):
        return result
    return {
        **result,
        "status": "submission_unknown",
        "job_id": job_id,
        "status_path": "/jobs/" + job_id,
        "hint": "Read this job_id with job_status or job_list before deciding any next submission.",
    }


def artifact(args, request):
    return read_batch(args, request)


def collection(results):
    """Report every item of a batch, never hiding partial success."""
    items = [tool_results.normalize(value) for value in results]
    failed = [item for item in items if not item["ok"]]
    result = {"items": items, "count": len(items), "failed": len(failed)}
    if failed:
        result["notice"] = "Read every item; some submissions in this batch did not succeed."
        if len(failed) == len(items):
            result["error"] = "batch_failed"
            result["hint"] = (
                "No submission in this batch succeeded; correct each item error before retrying."
            )
    return result


def batch(name, args, request, root):
    """Submit or prepare several independent jobs in one decision turn."""
    if "prepared_ids" in args:
        if name != "job_submit":
            fail("invalid_batch", "job_prepare works from jobs=[...], not prepared_ids.")
        arguments(args, {"prepared_ids"})
        identifiers = args["prepared_ids"]
        valid = (
            isinstance(identifiers, list)
            and 1 <= len(identifiers) <= MAX_BATCH
            and all(isinstance(value, str) and PREPARED.fullmatch(value) for value in identifiers)
        )
        if not valid:
            fail(
                "invalid_prepared_ids",
                f"Submit 1–{MAX_BATCH} exact prepared_id values saved by job_prepare.",
            )
        from . import job_preparation

        return collection([job_preparation.submit(value, request, root) for value in identifiers])
    arguments(args, {"jobs"})
    entries = args["jobs"]
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_BATCH
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        fail("invalid_jobs", f"Provide 1–{MAX_BATCH} job objects in jobs, each with a command.")
    from . import job_preparation

    results = []
    for entry in entries:
        try:
            if name == "job_prepare":
                job = submit_request(entry, root, default_gpu=True)
                if not job["gpu"]:
                    results.append(
                        {
                            "error": "gpu_preparation_required",
                            "hint": "Submit CPU work directly with job_submit(jobs=[...]).",
                        }
                    )
                    continue
                results.append(job_preparation.prepare(job, request))
            else:
                job = submit_request(entry, root)
                if job["gpu"]:
                    results.append(
                        {
                            "error": "gpu_job_needs_preparation",
                            "hint": GPU_NEEDS_PREPARATION,
                        }
                    )
                    continue
                results.append(submission_result(request("POST", "/jobs", job)))
        except ToolInputError as error:
            results.append({"error": error.code, "hint": error.hint})
    return collection(results)


def dispatch(name, args, request, *, workspace=None, cursor=None):
    try:
        if name == "benchmark":
            return benchmark(args, request, workspace or Path.cwd())
        if name == "benchmark_group":
            from .benchmark_groups import advance_group

            arguments(args, {"group_id", "timeout_seconds"})
            timeout = integer(args.get("timeout_seconds", 0), "timeout_seconds", 0, 300)
            return advance_group(
                args.get("group_id"), request, workspace or Path.cwd(), timeout, cursor=cursor
            )
        if name in {"job_prepare", "job_submit"}:
            from . import job_preparation

            root = Path(workspace or Path.cwd())
            if "jobs" in args or "prepared_ids" in args:
                return batch(name, args, request, root)
            if name == "job_prepare":
                job = submit_request(args, root, default_gpu=True)
                if not job["gpu"]:
                    fail(
                        "gpu_preparation_required",
                        "Use job_submit(command,attachments) directly for CPU work.",
                    )
                return job_preparation.prepare(job, request)
            if "prepared_id" in args:
                arguments(args, {"prepared_id"})
                return job_preparation.submit(args["prepared_id"], request, root)
            job = submit_request(args, root)
            if job["gpu"]:
                # A prepared step is procedure, not a decision: do it here instead of
                # making the model remember two calls for the same request.
                prepared = job_preparation.prepare(job, request)
                if prepared.get("error"):
                    return prepared
                return {
                    **prepared,
                    "status": "prepared_pending_preflight",
                    "hint": (
                        "The GPU request is prepared and its CPU preflight is running. When "
                        "preflight_job_id finishes cleanly, call job_submit(prepared_id=...)."
                    ),
                }
            return submission_result(request("POST", "/jobs", job))
        if name == "a3_api":
            return a3_api(args, request, Path(workspace or Path.cwd()))
        if name == "dataset_audit":
            return dataset_audit(args, request, Path(workspace or Path.cwd()))
        if name == "job_artifact":
            return artifact(args, request)
        if name == "job_list":
            arguments(args, {"scope", "after", "limit"})
            parameters = dict(args)
            if "limit" in parameters:
                integer(parameters["limit"], "limit", 1, 200)
            for field in ("scope", "after"):
                if field in parameters:
                    text(parameters[field], field, 200)
            return request("GET", "/jobs" + ("?" + urlencode(parameters) if parameters else ""))
        if name in {"job_status", "job_cancel"}:
            arguments(args, {"job_id"})
            path = "/jobs/" + identifier(args.get("job_id"))
            if name == "job_status":
                return request("GET", path)
            if name == "job_cancel":
                return request("DELETE", path)
        fail("unknown_job_tool", "Use one of the registered job tools.")
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
