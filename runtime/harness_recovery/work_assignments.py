"""Bounded worker packages and durable coordination, using existing Host permissions."""

import base64
import hashlib
import json
import os
import pathlib
import re
import stat
import time
import uuid
from urllib.parse import urlencode, urlsplit

import context_manager

STATE = pathlib.Path("orchestration/assignments.json")
CORE_FILES = (
    "agent.py",
    "compaction.py",
    "recall.py",
    "spill.py",
    "context_manager.py",
    "token_count.py",
    "memory_state.py",
    "analyzer_client.py",
    "work_assignments.py",
    "skill_tools.py",
    "web_tools.py",
)
WORKER_INSTRUCTIONS = (
    "You are a bounded {role} worker, not the main coordinator. Complete only this "
    "assignment using your own tools and identity. Read assigned inputs and relevant "
    "frozen protocol; do not inherit the main conversation or claim whole-run acceptance. "
    "Policy workers implement data/training/evaluation; Harness workers implement and test "
    "Harness changes. Preserve original evidence, inputs and code hashes. GPU model work "
    "must use authorized gpu:true jobs; do not alter Host limits or other identities. "
    "Return worker-result.json with id, role, status complete|blocked, summary, evidence "
    "entries with workspace-relative local path and sha256 (remote /outputs paths are not "
    "local evidence), and exports listing ready IDs granted to current main. "
    "Use only these report fields, summary at most 4000 characters; put detailed logs in evidence files. Include every "
    "declared deliverable. A report is a claim for the coordinator to assess, not proof "
    "of adoption. Use worker-task.coordinator_session_id as the explicit session_id in "
    "export grants; to_main is not an API alias. This ID is not a credential. If the "
    "recipient changed, report that boundary instead of guessing or bypassing scope. "
    "The operational worker_delivery frame explains missing/invalid reports; correct only "
    "actual delivery defects without changing results. "
    "Report paths are relative to your worker workspace. After collection the "
    "coordinator must use evidence_paths, not reinterpret your archive paths as its own. "
    "Save recoverable job/asset references before exiting. Do not start "
    "unassigned work, manufacture results or try to become main."
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class HostRequestFailed(ValueError):
    def __init__(self, status=None):
        super().__init__("Host request failed; inspect existing state before retry")
        self.status = status


def checked(req, method, path, data=None):
    result = req(method, path, data)
    if not isinstance(result, dict):
        raise HostRequestFailed()
    if result.get("http_error"):
        raise HostRequestFailed(result["http_error"])
    return result


def is_main(req):
    own = checked(req, "GET", "/sessions/self")
    evolution = req("GET", "/evolution")
    if isinstance(evolution, dict) and evolution.get("http_error") == 403:
        # Ordinary workers are deliberately excluded from evolution state. A valid
        # self identity plus this specific Host denial identifies a non-main role.
        return False
    if not isinstance(evolution, dict) or evolution.get("http_error"):
        raise ValueError("Cannot verify current main identity")
    return bool(own.get("session_id")) and own["session_id"] == evolution.get("main_owner")


def load():
    value = json.loads(STATE.read_text()) if STATE.exists() else {"version": 1, "assignments": {}}
    if value.get("version") != 1 or not isinstance(value.get("assignments"), dict):
        raise ValueError("Invalid assignment ledger; preserve and inspect it")
    return value


def save(value):
    context_manager.atomic(STATE, json.dumps(value, ensure_ascii=False))


def wait_cursor(messages):
    """Only completed wait results acknowledge a notification in existing history."""
    calls = set()
    cursor = None
    for message in messages:
        if message.get("type") == "function_call" and message.get("name") == "wait":
            calls.add(message.get("call_id"))
        if message.get("type") != "function_call_output" or message.get("call_id") not in calls:
            continue
        raw = message.get("output")
        if not isinstance(raw, str) or len(raw) > 16000:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and value.get("wait_schema") == 1
            and isinstance(value.get("wake_cursor"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["wake_cursor"])
        ):
            cursor = value["wake_cursor"]
    return cursor


def wait_for_change(
    req,
    seconds=5,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    manifest_path=pathlib.Path("/run/input/input-manifest.json"),
    signals=None,
    cursor=None,
):
    """Wait without a model round trip; observations never acknowledge Host events."""
    if type(seconds) is not int or not 0 <= seconds <= 600:
        raise ValueError("Wait seconds must be an integer in 0..600")
    start = clock()
    deadline = start + seconds
    current_cursor = cursor
    last_signal = None
    next_status = start
    previous = None

    def result(reason, **fields):
        value = {"wait_schema": 1, "reason": reason, "elapsed_seconds": clock() - start, **fields}
        if current_cursor is not None:
            value["wake_cursor"] = current_cursor
        return value

    def notification():
        nonlocal current_cursor, last_signal
        if signals is None or last_signal is not None and clock() - last_signal < 2:
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        try:
            value = signals(min(1, remaining))
        except (OSError, TimeoutError):
            return result("observation_error", path="/wait/signals")
        last_signal = clock()
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or not isinstance(value.get("cursor"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["cursor"])
            or type(value.get("pending")) is not bool
        ):
            return result(
                "observation_error",
                path="/wait/signals",
                http_error=value.get("http_error") if isinstance(value, dict) else None,
            )
        changed = (
            value["cursor"] != current_cursor if current_cursor is not None else value["pending"]
        )
        current_cursor = value["cursor"]
        if changed:
            return result(
                "notification",
                notifications={
                    key: value.get(key) for key in ("feedback", "analysis", "inputs", "is_main")
                },
            )
        return None

    assignments = load()["assignments"]
    if any(x.get("status") == "awaiting_decision" for x in assignments.values()):
        return result("decision_pending")
    paths = []
    for item in assignments.values():
        session = item.get("session_id")
        if (
            item.get("status") == "assigned"
            and isinstance(session, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session)
        ):
            paths.append("/sessions/" + session)
    operational = pathlib.Path("operational-state.json")
    if operational.exists():
        for job, item in json.loads(operational.read_text()).get("jobs", {}).items():
            if item.get("status") not in {"completed", "cancelled", "failed"} and re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", job
            ):
                paths.append("/jobs/" + job)

    def inputs():
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text())
        return {key: manifest.get(key) for key in ("status", "revision", "generation", "digest")}

    if len(paths) > 64:
        return result("observation_error", error="too_many_wait_targets")
    initial_inputs = inputs()
    if initial_inputs is not None and initial_inputs.get("status") != "active":
        return result("inputs_changed")
    while True:
        if inputs() != initial_inputs:
            return result("inputs_changed")
        notice = notification()
        if notice:
            return notice
        if clock() >= deadline:
            return result("deadline", observed=previous or {})
        if clock() >= next_status:
            current = {}
            for path in sorted(set(paths)):
                notice = notification()
                if notice:
                    return notice
                remaining = deadline - clock()
                if remaining <= 0:
                    return result("deadline", observed={**(previous or {}), **current})
                try:
                    value = req("GET", path, timeout=min(2 if signals else 10, remaining))
                except (OSError, TimeoutError):
                    return result("observation_error", path=path)
                if (
                    not isinstance(value, dict)
                    or value.get("http_error")
                    or not isinstance(value.get("status"), str)
                    or not value["status"]
                ):
                    return result(
                        "observation_error",
                        path=path,
                        http_error=value.get("http_error") if isinstance(value, dict) else None,
                    )
                current[path] = {key: value.get(key) for key in ("status", "exit_code", "closed")}
            terminal = any(
                x.get("status") not in {"running", "created", "queued", "pending", "starting"}
                or x.get("closed")
                for x in current.values()
            )
            if terminal or previous is not None and current != previous:
                return result("state_changed", changes=current)
            previous = current
            next_status = clock() + 10
        now = clock()
        if now >= deadline:
            return result("deadline", observed=previous or {})
        sleep(min(2 if signals else 10, deadline - now, max(0, next_status - now)))


def relative_path(name):
    if not isinstance(name, str) or not name or "\0" in name:
        raise ValueError("Expected a relative file path")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path == pathlib.PurePosixPath("."):
        raise ValueError("Unsafe assignment path")
    if any(
        part.startswith(".")
        or any(word in part.lower() for word in ("credential", "secret", "token", "password"))
        for part in path.parts
    ):
        raise ValueError("Private/control files cannot be assignment context")
    return path.as_posix()


def text_file(name):
    path = pathlib.Path(name)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Worker source must be a regular local file")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Assignment source too large; use explicit sealed exports")
    return raw.decode("utf-8")


def assignment_contract(arguments):
    identifier = arguments["id"]
    if not isinstance(identifier, str):
        raise TypeError("Assignment id must be text")
    role = arguments.get("role")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier) or role not in {
        "policy",
        "harness",
    }:
        raise ValueError("Assignment needs a bounded id and policy|harness role")
    for field, limit in (("objective", 8000), ("stop_condition", 4000)):
        if not isinstance(arguments.get(field), str) or not 1 <= len(arguments[field]) <= limit:
            raise ValueError("Assignment needs a bounded objective and stop condition")
    deliverables = arguments.get("deliverables")
    context = arguments.get("context_files", [])
    exports = arguments.get("read_only_exports", [])
    if not isinstance(deliverables, list) or not 1 <= len(deliverables) <= 16:
        raise ValueError("Assignment needs 1..16 relative deliverables")
    if (
        not isinstance(context, list)
        or len(context) > 32
        or not isinstance(exports, list)
        or len(exports) > 64
    ):
        raise ValueError("Assignment context or export count exceeds bound")
    if any(not isinstance(x, str) or not re.fullmatch(r"export-[a-f0-9]{32}", x) for x in exports):
        raise ValueError("Expected explicit ready export IDs, never private asset paths")
    return {
        "id": identifier,
        "role": role,
        "objective": arguments["objective"],
        "stop_condition": arguments["stop_condition"],
        "deliverables": [relative_path(x) for x in deliverables],
        "context_files": context,
        "read_only_exports": exports,
    }


def fixed_skill_evidence(files):
    """Only the existing fixed backend's declared, hash-verified data assets."""
    raw = files.get("skills/index.json")
    if raw is None:
        return set()
    catalog = strict_package_json(raw)
    entries = catalog.get("skills", []) if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Invalid fixed-skill evidence contract")  # noqa: TRY004 — stable safe validation code
    selected = [
        e for e in entries if isinstance(e, dict) and e.get("id") == "synthetic-report-checker"
    ]
    if len(selected) > 1:
        raise ValueError("Invalid fixed-skill evidence contract")
    if not selected:
        return set()
    entry = selected[0]
    names, hashes = entry.get("evidence"), entry.get("evidence_sha256")
    if (
        entry.get("status") != "local-function"
        or entry.get("local_source") != "skill_tools.py:check_report"
        or entry.get("evidence_source_sha256") != digest(files["skill_tools.py"].encode())
        or not isinstance(names, list)
        or not 1 <= len(names) <= 16
        or any(not isinstance(n, str) for n in names)
        or len(set(names)) != len(names)
        or not isinstance(hashes, dict)
        or set(hashes) != set(names)
    ):
        raise ValueError("Invalid fixed-skill evidence contract")
    for name in names:
        if (
            relative_path(name) != name
            or "\\" in name
            or not name.startswith(("skills/", "skills-evidence/"))
            or not isinstance(files.get(name), str)
            or hashes[name] != digest(files[name].encode())
        ):
            raise ValueError("Invalid fixed-skill evidence contract")
    return set(names)


def worker_package(arguments, *, coordinator_session_id=None):
    contract = assignment_contract(arguments)
    identifier, role = contract["id"], contract["role"]
    context, exports = contract["context_files"], contract["read_only_exports"]
    deliverables = contract["deliverables"]
    files = {name: text_file(name) for name in CORE_FILES}
    for name in context:
        name = relative_path(name)
        if not (
            pathlib.PurePosixPath(name).parts[0] == "research"
            or name.startswith("orchestration/results/")
        ):
            raise ValueError("Select research context only; never copy checkpoint or private logs")
        files[name] = text_file(name)
    task = {
        "version": 1,
        "assignment_nonce": uuid.uuid4().hex,
        "coordinator_session_id": coordinator_session_id,
        "id": identifier,
        "role": role,
        "objective": arguments["objective"],
        "stop_condition": arguments["stop_condition"],
        "deliverables": [relative_path(x) for x in deliverables],
        "context_files": context,
        "read_only_exports": exports,
        "source_hashes": {n: digest(s.encode()) for n, s in files.items()},
        "frozen_inputs": "/run/input/input-manifest.json",
        "result_path": "worker-result.json",
    }
    files["worker-task.json"] = json.dumps(task, ensure_ascii=False)
    files["checkpoint.json"] = json.dumps(
        [
            {
                "role": "user",
                "content": WORKER_INSTRUCTIONS.format(role=role) + "\n" + files["worker-task.json"],
            }
        ],
        ensure_ascii=False,
    )
    selected = {
        "research-orchestrator",
        "a3-mujoco-autoresearch" if role == "policy" else "rsi-harness-evolution",
    }
    index_path = pathlib.Path("skills/index.json")
    entries = []
    if index_path.is_file():
        catalog = json.loads(index_path.read_text())
        for entry in catalog.get("skills", []):
            if (
                entry.get("id") == "synthetic-report-checker"
                and entry.get("status") == "local-function"
            ):
                # Preserve the existing F backend's fixed allowlist and evidence;
                # copying metadata never grants a new executable skill.
                evidence = entry.get("evidence", [])
                if not isinstance(evidence, list) or len(evidence) > 16:
                    raise ValueError("Invalid local-skill evidence list")
                for name in evidence:
                    name = relative_path(name)
                    if not name.startswith(("skills/", "skills-evidence/")):
                        raise ValueError("Local skill evidence must be bounded skill assets")
                    files[name] = text_file(name)
                entries.append(entry)
                continue
            if (entry.get("reference_id", entry.get("id")) not in selected) or entry.get(
                "status"
            ) != "reference":
                continue
            source = relative_path(entry["local_source"])
            if pathlib.PurePosixPath(source).parts[0] != "skills":
                raise ValueError("Reference source must be inside skills")
            directory = pathlib.Path(source).parent
            for path in sorted(directory.rglob("*")):
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.is_symlink():
                    raise ValueError("Skill sources cannot be symlinks")
                if path.is_file():
                    files[path.as_posix()] = text_file(path.as_posix())
            if digest(files[source].encode()) != entry.get("source_sha256"):
                raise ValueError("Reference source hash changed; reconcile before dispatch")
            entries.append(entry)
    files["skills/index.json"] = json.dumps({"version": 1, "skills": entries})
    fixed_skill_evidence(files)
    if len(files) > 128 or sum(len(s.encode()) for s in files.values()) > 20 * 1024 * 1024:
        raise ValueError("Worker package exceeds existing Host upload bound")
    return task, files


def strict_package_json(raw):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate prepared package JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-JSON prepared package constant")

    try:
        return json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Invalid prepared package JSON") from error


def bounded_preparation_bytes(name):
    """Descriptor-relative bounded read of an already selected canonical path."""
    directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    leaf = None
    try:
        parts = pathlib.PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Prepared source must be a regular file of at most 1 MiB")
        with os.fdopen(leaf, "rb") as stream:
            leaf = None
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Prepared source size or SHA256 mismatch")
        return raw
    except OSError as error:
        raise ValueError("Prepared source is unavailable or unsafe") from error
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(directory)


def collected_bytes(state, name, expected):
    """Bind bytes to an existing current or hash-verified archived collection receipt."""
    if (
        relative_path(name) != name
        or "\\" in name
        or not name.startswith("orchestration/results/")
        or not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
    ):
        raise ValueError("Prepared source needs a canonical collected path and SHA256")

    def matching_owners(records):
        owners = []
        for item in records:
            if item.get("status") not in {"awaiting_decision", "decided"}:
                continue
            records = [(item, item.get("evidence_source_session") or item.get("session_id"))]
            records.extend(
                (receipt, receipt.get("source_session"))
                for receipt in item.get("artifact_collections", [])
            )
            for record, source in records:
                for original, delivered in record.get("evidence_paths", {}).items():
                    if (
                        delivered == name
                        and record.get("evidence_hashes", {}).get(original) == expected
                    ):
                        owners.append(source)
        return owners

    owners = matching_owners(state["assignments"].values())
    if not owners:
        identifier = pathlib.PurePosixPath(name).parts[2]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
            raise ValueError("Invalid archived preparation assignment")
        records = []
        for path in pathlib.Path("orchestration/archive").glob(identifier + "-*.json"):
            match = re.fullmatch(re.escape(identifier) + r"-([a-f0-9]{64})\.json", path.name)
            if match is None:
                continue  # Another valid assignment may share this identifier prefix.
            if len(records) >= 16:
                raise ValueError("Too many matching preparation archives; select current receipt")
            expected_digest = match[1]
            archived = bounded_preparation_bytes(path.as_posix())
            if digest(archived) != expected_digest:
                raise ValueError("Archived preparation receipt SHA256 mismatch")
            record = strict_package_json(archived)
            if not isinstance(record, dict) or record.get("id") != identifier:
                raise ValueError("Archived preparation receipt identity mismatch")
            records.append(record)
        owners = matching_owners(records)
    owners = list(set(owners))
    if len(owners) != 1 or not isinstance(owners[0], str):
        raise ValueError("Prepared source lacks a unique verified collection receipt")
    raw = bounded_preparation_bytes(name)
    if digest(raw) != expected:
        raise ValueError("Prepared source SHA256 mismatch")
    return raw, owners[0]


def prepared_worker_package(state, arguments, coordinator):
    """Validate untrusted data; never import the producer or inherit its checkpoint."""
    contract = assignment_contract(arguments)
    if not all(
        isinstance(arguments.get(k), str)
        for k in ("package_path", "package_sha256", "module_path", "module_sha256")
    ):
        raise ValueError(
            "Prepared validation needs collected package and module paths with SHA256 pins"
        )
    if contract["role"] != "harness":
        raise ValueError("Prepared packages are bounded Harness validation only")
    raw, producer = collected_bytes(state, arguments["package_path"], arguments["package_sha256"])
    _, module_owner = collected_bytes(state, arguments["module_path"], arguments["module_sha256"])
    package = strict_package_json(raw)
    if (
        not isinstance(package, dict)
        or set(package) != {"version", "module_sha256", "task", "files"}
        or type(package["version"]) is not int
        or package["version"] != 1
        or package["module_sha256"] != arguments["module_sha256"]
    ):
        raise ValueError("Invalid prepared package envelope or module identity")
    task, files = package["task"], package["files"]
    if not isinstance(task, dict) or not isinstance(files, dict) or len(files) > 128:
        raise ValueError("Invalid prepared task or file count")
    expected_fields = set(contract) | {
        "version",
        "assignment_nonce",
        "coordinator_session_id",
        "source_hashes",
        "frozen_inputs",
        "result_path",
    }
    if (
        set(task) != expected_fields
        or type(task["version"]) is not int
        or task["version"] != 1
        or any(task.get(k) != v for k, v in contract.items())
        or task["coordinator_session_id"] != coordinator
        or task["frozen_inputs"] != "/run/input/input-manifest.json"
        or task["result_path"] != "worker-result.json"
        or not isinstance(task["assignment_nonce"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", task["assignment_nonce"])
    ):
        raise ValueError("Prepared task differs from the main's explicit assignment")
    contexts = []
    for spec in contract["context_files"]:
        if isinstance(spec, str) and spec.startswith("sha256:"):
            fields = spec.split(":", 2)
            if len(fields) != 3 or not re.fullmatch(r"[a-f0-9]{64}", fields[1]):
                raise ValueError("Invalid prepared context hash selector")
            name = fields[2]
            if not name.startswith("operator-recovered-artifacts/"):
                raise ValueError("Prepared hash context must select recovered research artifacts")
            text = files.get(name)
            if not isinstance(text, str) or digest(text.encode()) != fields[1]:
                raise ValueError("Prepared context SHA256 mismatch")
            strict_package_json(text)
        else:
            name = spec
            if not isinstance(name, str) or not name.startswith(
                ("research/", "orchestration/results/")
            ):
                raise ValueError("Prepared context must select explicit research files")
        if relative_path(name) != name or "\\" in name or name in contexts:
            raise ValueError("Prepared context must be canonical and unique")
        if not spec.startswith("sha256:"):
            if name.startswith("orchestration/results/"):
                try:
                    selected, _ = collected_bytes(state, name, digest(files.get(name, "").encode()))
                except ValueError as error:
                    raise ValueError("Prepared context lacks matching collected content") from error
            else:
                selected = bounded_preparation_bytes(name)
            if not isinstance(files.get(name), str) or selected != files[name].encode():
                raise ValueError("Prepared context differs from the main's selected input")
        contexts.append(name)
    sources = set(CORE_FILES) | set(contexts)
    hashes = task["source_hashes"]
    if not isinstance(hashes, dict) or set(hashes) != sources:
        raise ValueError("Prepared source manifest differs from selected files")
    skill_evidence = fixed_skill_evidence(files)
    size = 0
    for name, text in files.items():
        if not isinstance(text, str):
            raise TypeError("Prepared files must be UTF8 text")
        if name not in CORE_FILES and (relative_path(name) != name or "\\" in name):
            raise ValueError("Prepared files must have canonical relative names")
        if name not in sources | skill_evidence | {
            "worker-task.json",
            "checkpoint.json",
        } and not name.startswith("skills/"):
            raise ValueError("Undeclared prepared file")
        encoded = text.encode()
        size += len(encoded)
        if len(encoded) > 1024 * 1024:
            raise ValueError("Prepared file exceeds 1 MiB")
        if name in sources and digest(encoded) != hashes[name]:
            raise ValueError("Prepared source hash mismatch")
    if size > 20 * 1024 * 1024 or not sources.issubset(files):
        raise ValueError("Prepared files exceed bounds or omit core/context files")
    original_task = files.get("worker-task.json", "")
    if strict_package_json(original_task) != task or strict_package_json(
        files.get("checkpoint.json", "")
    ) != [
        {
            "role": "user",
            "content": WORKER_INSTRUCTIONS.format(role=task["role"]) + "\n" + original_task,
        }
    ]:
        raise ValueError("Prepared control files must contain only the fresh bounded task")
    task = {**task, "assignment_nonce": uuid.uuid4().hex}
    files = dict(files)
    files["worker-task.json"] = json.dumps(task, ensure_ascii=False)
    files["checkpoint.json"] = json.dumps(
        [
            {
                "role": "user",
                "content": WORKER_INSTRUCTIONS.format(role=task["role"])
                + "\n"
                + files["worker-task.json"],
            }
        ],
        ensure_ascii=False,
    )
    receipt = {
        k: arguments[k] for k in ("package_path", "package_sha256", "module_path", "module_sha256")
    }
    receipt.update(
        source_session=producer,
        module_source_session=module_owner,
        generation_proven=False,
        scope="Prepared packaging data and ordinary Host dispatch; not candidate dispatch execution",
    )
    return task, files, receipt


def artifact(req, session, name, *, bounded=False):
    query = {"path": name}
    if bounded:
        query["limit"] = 1024 * 1024 + 1  # Detect overflow without fetching an unbounded file.
    result = checked(req, "GET", "/sessions/" + session + "/artifacts?" + urlencode(query))
    raw = base64.b64decode(result["data"], validate=True)
    if len(raw) > 1024 * 1024:
        raise ValueError("Worker report too large; use bounded reports and sealed exports")
    return raw


def collect_child_artifacts(req, state, arguments):
    """Recover research outputs from a collected worker's own stopped descendants.

    Reauthorize every read with Host; local archive names never grant access. A
    separate receipt supplements delivery, without rewriting the producer's report
    or turning its prior blocked/follow-up decision into acceptance.
    """
    identifier = arguments.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
    ):
        raise ValueError("Expected an existing bounded assignment id")
    item = state["assignments"].get(identifier)
    if not item or item.get("status") not in {"awaiting_decision", "decided"}:
        raise ValueError("Collect the original worker report before supplemental artifacts")
    producer = item.get("session_id")
    source = arguments.get("source_session_id")
    if not isinstance(source, str) or not re.fullmatch(r"candidate-[a-f0-9]{32}", source):
        raise ValueError("Expected an explicit candidate source session")
    references = arguments.get("artifacts")
    if not isinstance(references, list) or not 1 <= len(references) <= 16:
        raise ValueError("Supplemental collection needs 1..16 research artifacts")
    selected = {}
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError("Artifact needs only its exact research path and SHA256")
        name, expected = reference["path"], reference["sha256"]
        if (
            relative_path(name) != name
            or not name.startswith("research/")
            or "\\" in name
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or name in selected
        ):
            raise ValueError("Select unique canonical research paths with full SHA256 pins")
        selected[name] = expected
    # GET /sessions is already filtered by the caller's Host ancestry ACL. Also
    # require the narrower original producer subtree, not just any main descendant.
    sessions = checked(req, "GET", "/sessions").get("sessions", [])
    parents = {x["session_id"]: x.get("parent") for x in sessions}
    chain, cursor = [], source
    while cursor != producer:
        if cursor not in parents or cursor in chain or len(chain) >= 128:
            raise ValueError("Artifact source must descend from the recorded producer worker")
        chain.append(cursor)
        cursor = parents[cursor]
    if not chain or producer not in parents:
        raise ValueError("Artifact source must be a producer descendant, not the producer itself")
    if checked(req, "GET", "/sessions/" + source).get("status") not in {"exited", "closed"}:
        raise ValueError("Wait for the artifact-producing session to stop before collection")
    batches = item.get("artifact_collections", [])
    key = digest(json.dumps([source, selected], sort_keys=True).encode())
    existing = next((x for x in batches if x.get("collection_sha256") == key), None)
    if existing is None and len(batches) >= 16:
        raise ValueError("Supplemental collection receipt limit reached")
    pending, paths, total = {}, {}, 0
    for name, expected in selected.items():
        raw = artifact(req, source, name, bounded=True)
        raw.decode("utf-8")  # Existing prepared/worker interfaces are text-only.
        total += len(raw)
        if digest(raw) != expected or total > 8 * 1024 * 1024:
            raise ValueError("Supplemental artifact SHA256 or total size mismatch")
        target = (
            pathlib.Path("orchestration/results") / identifier / "child-artifacts" / source / name
        )
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError("Supplemental artifact destination must not contain symlinks")
        if target.exists() and (not target.is_file() or target.read_bytes() != raw):
            raise ValueError("Supplemental artifact conflicts with preserved local evidence")
        paths[name] = target.as_posix()
        pending[target] = raw
    # Validate all reads before writing. Publish the ledger receipt last: a write
    # failure can leave exact unreferenced files, never a false successful receipt.
    for target, raw in pending.items():
        if not target.exists():
            context_manager.atomic(target, raw)
    receipt = {
        "collection_sha256": key,
        "source_session": source,
        "producer_session": producer,
        "ancestry": chain + [producer],
        "evidence_paths": paths,
        "evidence_hashes": selected,
        "bytes": total,
        "generation_proven": False,
        "scope": "Verified supplemental bytes; not execution attestation or scientific acceptance",
    }
    if existing is not None:
        if existing != receipt:
            raise ValueError("Supplemental collection conflicts with its preserved receipt")
        return receipt
    item["artifact_collections"] = [*batches, receipt]
    save(state)
    return receipt


def collect_report_references(req, session, identifier, report_texts, declared):
    """Copy bounded, hash-addressed tool archives cited by this worker's reports.

    Optional citations are not declared evidence or scientific acceptance. Keep
    their availability explicit without inventing local files or blocking a
    truthful partial delivery. Never fetch arbitrary paths or conversation snapshots.
    """
    references = sorted(
        set(re.findall(r"context-history/tool-output-[0-9a-f]{64}\.json", "\n".join(report_texts)))
        - set(declared)
    )
    hashes, delivery, remaining = {}, {}, 1024 * 1024
    if len(references) > 8:
        delivery["__omitted__"] = {"status": "limit_exceeded", "count": len(references) - 8}
    # At most eight existing artifact requests (each <=1 MiB admitted by artifact),
    # and at most 1 MiB of verified copies. Transport is bounded per request,
    # not misrepresented as a shared 1 MiB network quota.
    for name in references[:8]:
        if remaining <= 0:
            delivery[name] = {"status": "limit_exceeded"}
            continue
        try:
            raw = artifact(req, session, name)
            expected = pathlib.PurePosixPath(name).stem.rsplit("-", 1)[-1]
            if digest(raw) != expected or len(raw) > remaining:
                raise ValueError("Unverified optional report archive")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("type") != "function_call_output":
                raise ValueError("Report reference is not a tool-output archive")
        except (HostRequestFailed, ValueError, KeyError):
            delivery[name] = {"status": "unavailable"}
            continue
        target = pathlib.Path("orchestration/results") / identifier / name
        context_manager.atomic(target, raw.decode("utf-8"))
        remaining -= len(raw)
        hashes[name] = expected
        delivery[name] = {
            "status": "verified_copy",
            "path": target.as_posix(),
            "source_session": session,
            "sha256": expected,
        }
    return hashes, delivery


def collect(req, state, identifier):
    item = state["assignments"][identifier]
    if item["status"] == "decided":
        # An explicit review can detect changed evidence without turning a closed
        # decision back into an open task or rewriting the earlier conclusion.
        for name, expected in item.get("evidence_hashes", {}).items():
            if digest(artifact(req, item["session_id"], name)) != expected:
                raise ValueError("Worker evidence hash mismatch; prior decision remains recorded")
        return item
    session = item.get("session_id")
    if not session:
        raise ValueError("Creation uncertain: reconcile /sessions before any new submission")
    actual_task = json.loads(artifact(req, session, "worker-task.json"))
    if digest(json.dumps(actual_task, sort_keys=True, ensure_ascii=False).encode()) != item.get(
        "task_sha256"
    ):
        raise ValueError("Worker task identity differs; do not consume another assignment's result")
    status = checked(req, "GET", "/sessions/" + session)
    if status.get("status") not in {"exited", "closed"}:
        return {**item, "worker_status": status.get("status"), "decision_ready": False}
    try:
        report = json.loads(artifact(req, session, "worker-result.json"))
    except HostRequestFailed as error:
        if error.status not in (404, 422):
            raise
        item.update(
            status="awaiting_decision",
            delivery_failed=True,
            scientific_success=None,
            evidence_hashes={},
            worker_status=status.get("status"),
            worker_exit_code=status.get("exit_code"),
            failure="Worker exited without a readable required report; remote assets remain unknown",
        )
        save(state)
        return item
    if (
        not isinstance(report, dict)
        or set(report) - {"id", "role", "status", "summary", "evidence", "exports"}
        or not isinstance(report.get("summary"), str)
        or len(report["summary"]) > 4000
    ):
        raise ValueError("Worker needs a bounded report; place detailed logs in evidence files")
    if (
        report.get("id") != identifier
        or report.get("role") != item["role"]
        or report.get("status") not in {"complete", "blocked"}
    ):
        raise ValueError("Worker result does not match its bounded assignment")
    evidence = report.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 16:
        raise ValueError("Worker result needs bounded evidence references")
    hashes = {}
    report_texts = []
    for reference in evidence:
        name = relative_path(reference["path"])
        raw = artifact(req, session, name)
        if reference.get("sha256") != digest(raw):
            raise ValueError("Worker evidence hash mismatch; no adoption decision recorded")
        target = pathlib.Path("orchestration/results") / identifier / name
        context_manager.atomic(target, raw.decode("utf-8"))
        hashes[name] = digest(raw)
        if name.endswith((".md", ".json", ".txt")):
            report_texts.append(raw.decode("utf-8"))
    reference_hashes, reference_delivery = collect_report_references(
        req, session, identifier, report_texts, hashes
    )
    if report["status"] == "complete" and not set(item["deliverables"]) <= hashes.keys():
        raise ValueError("Worker completion omits required deliverables")
    exports = report.get("exports", [])
    if not isinstance(exports, list) or len(exports) > 64:
        raise ValueError("Invalid returned exports")
    for export in exports:
        if not isinstance(export, str) or not re.fullmatch(r"export-[a-f0-9]{32}", export):
            raise ValueError("Invalid returned export ID")
        if checked(req, "GET", "/jobs/exports/" + export).get("status") != "ready":
            raise ValueError("Returned export is not ready and explicitly accessible to main")
    item.update(
        status="awaiting_decision",
        worker_status=status.get("status"),
        result=report,
        evidence_hashes=hashes,
        reference_hashes=reference_hashes,
        reference_delivery=reference_delivery,
        evidence_paths={
            name: (pathlib.PurePosixPath("orchestration/results") / identifier / name).as_posix()
            for name in {**hashes, **reference_hashes}
        },
        evidence_source_session=session,
        exports=exports,
    )
    save(state)
    return item


def view(state=None):
    state = load() if state is None else state
    items = list(state["assignments"].values())
    pending = [item for item in items if item["status"] != "decided"]
    return {
        "scope": "this_task",
        "pending": [
            {k: item.get(k) for k in ("id", "role", "status", "session_id", "delivery_failed")}
            for item in pending[:12]
        ],
        "omitted_pending": max(0, len(pending) - 12),
        "retained_assignments": len(items),
        "archived_assignments": state.get("archived_count", 0),
        "note": "Recorded operational assignments, not scientific acceptance. Use delegate collect/decide for evidence and decisions.",
    }


def lookup_assignment(state, identifier):
    """Describe ledger absence, never infer whether an external POST succeeded."""
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
    ):
        raise ValueError("Invalid assignment lookup identity")
    raw = STATE.read_bytes() if STATE.exists() else None
    source = {"path": str(STATE), "sha256": digest(raw) if raw is not None else None}
    state = json.loads(raw) if raw is not None else {"version": 1, "assignments": {}}
    if state.get("version") != 1 or not isinstance(state.get("assignments"), dict):
        raise ValueError("Invalid assignment ledger; preserve and inspect it")
    if identifier in state["assignments"]:
        return {
            "source": source,
            "found": True,
            "location": "current",
            "assignment": state["assignments"][identifier],
        }
    archived = []
    archive = pathlib.Path("orchestration/archive")
    if any(p.is_symlink() for p in (archive, *archive.parents)):
        raise ValueError("Assignment archive lookup is ambiguous or invalid")
    for path in archive.glob(identifier + "-*.json"):
        match = re.fullmatch(re.escape(identifier) + r"-([a-f0-9]{64})\.json", path.name)
        if not match:
            other = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})-([a-f0-9]{64})\.json", path.name
            )
            if other and other[1] != identifier:
                continue  # A different valid assignment may share the requested prefix.
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        if len(archived) >= 16:
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        try:
            data = bounded_preparation_bytes(path.as_posix())
            item = strict_package_json(data)
        except (ValueError, OSError) as error:
            raise ValueError("Assignment archive lookup is ambiguous or invalid") from error
        if digest(data) != match[1]:
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        if (
            not isinstance(item, dict)
            or item.get("id") != identifier
            or item.get("status") != "decided"
        ):
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        archived.append((path, item, match[1]))
    if len(archived) > 1:
        raise ValueError("Assignment archive lookup is ambiguous or invalid")
    if archived:
        path, item, sha = archived[0]
        return {
            "source": source,
            "found": True,
            "location": "archive",
            "assignment": item,
            "archive_source": {"path": path.as_posix(), "sha256": sha},
            "resubmitted": False,
        }
    return {
        "source": source,
        "found": False,
        "id": identifier,
        "scope": "current_and_verified_archived_assignment_ledger",
        "creation_status": "not_recorded_in_checked_ledger",
        "retry_safe": False,
        "resubmitted": False,
        "notice": "No record in this checked ledger. This is not a transport failure or proof that no Host session exists. Do not repeat the same lookup with unchanged source hash; use existing session receipts and pre-creation error evidence to decide next steps. Never blindly recreate an uncertain POST.",
    }


def dispatch(req, arguments):
    if not is_main(req):
        raise PermissionError("Only the current coordinator assigns and decides worker work")
    state = load()
    action = arguments.get("action")
    if action == "list":
        raw = STATE.read_bytes() if STATE.exists() else None
        if raw is not None:
            state = json.loads(raw)
            if state.get("version") != 1 or not isinstance(state.get("assignments"), dict):
                raise ValueError("Invalid assignment ledger; preserve and inspect it")
        source = {"path": str(STATE), "sha256": digest(raw) if raw is not None else None}
        identifier = arguments.get("id")
        if identifier is not None:
            return lookup_assignment(state, identifier)
        items = list(state["assignments"].values())
        pending = [x for x in items if x["status"] != "decided"]
        closed = [x for x in items if x["status"] == "decided"]
        selected = pending[:12] + closed[-6:]
        return {
            **view(state),
            "source": source,
            "detail": "Use delegate action=list with id for the complete unchanged record, or read the source ledger by pages. Reasons and evidence are omitted here, not deleted or reinterpreted.",
            "omitted_assignments": len(items) - len(selected),
            "assignments": [
                {
                    k: item.get(k)
                    for k in ("id", "role", "status", "session_id", "decision", "delivery_failed")
                }
                for item in selected
            ],
        }
    identifier = arguments.get("id")
    if action in {"assign", "assign_prepared"}:
        if identifier in state["assignments"]:
            raise ValueError("Assignment already recorded; inspect it, do not duplicate submission")
        while len(state["assignments"]) >= 64:
            archived = next(
                (
                    key
                    for key, value in state["assignments"].items()
                    if value["status"] == "decided"
                ),
                None,
            )
            if archived is None:
                raise ValueError("Too many unresolved assignments; close actual pending decisions")
            completed = state["assignments"][archived]
            raw = json.dumps(completed, ensure_ascii=False)
            context_manager.atomic(
                pathlib.Path("orchestration/archive")
                / (archived + "-" + digest(raw.encode()) + ".json"),
                raw,
            )
            state["assignments"].pop(archived)
            state["archived_count"] = state.get("archived_count", 0) + 1
        coordinator = checked(req, "GET", "/sessions/self").get("session_id")
        if not isinstance(coordinator, str) or not coordinator:
            raise ValueError("Cannot identify the coordinator for explicit export delivery")
        preparation = None
        if action == "assign_prepared":
            task, files, preparation = prepared_worker_package(state, arguments, coordinator)
        else:
            task, files = worker_package(arguments, coordinator_session_id=coordinator)
        item = {
            "id": identifier,
            "role": task["role"],
            "status": "creation_uncertain",
            "deliverables": task["deliverables"],
            "source_hashes": task["source_hashes"],
            "task_sha256": digest(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()),
        }
        if preparation is not None:
            item["prepared_validation"] = preparation
        state["assignments"][identifier] = item
        save(state)  # Reserve before network I/O; uncertainty must not replay creation.
        result = checked(
            req,
            "POST",
            "/sessions",
            {
                "command": ["python", "agent.py", "--resume", "--worker-task", "worker-task.json"],
                "files": files,
                "purpose": "worker",
                "read_only_exports": task["read_only_exports"],
            },
        )
        if not isinstance(result.get("session_id"), str):
            raise ValueError("Missing worker identity; reconcile existing sessions")
        item.update(status="assigned", session_id=result["session_id"], ledger_persisted=True)
        try:
            save(state)
        except OSError:
            # The Host already created this identity. Preserve its real result;
            # the durable pre-POST reservation and task hash allow reconciliation.
            item["ledger_persisted"] = False
        return item
    if action == "reconcile" and identifier not in state["assignments"]:
        return lookup_assignment(state, identifier)
    if identifier not in state["assignments"]:
        raise ValueError("Unknown assignment")
    if action == "reconcile":
        item = state["assignments"][identifier]
        if item.get("session_id"):
            return item
        sessions = checked(req, "GET", "/sessions?include_status=true").get("sessions", [])
        matches = []
        for session in sessions:
            try:
                task = json.loads(artifact(req, session["session_id"], "worker-task.json"))
            except (ValueError, KeyError):
                continue
            if task.get("id") == identifier and digest(
                json.dumps(task, sort_keys=True, ensure_ascii=False).encode()
            ) == item.get("task_sha256"):
                matches.append(session["session_id"])
        if len(matches) > 1:
            raise ValueError("Ambiguous existing workers; preserve and inspect both")
        if matches:
            item.update(status="assigned", session_id=matches[0])
            save(state)
        return {**item, "creation_confirmed": bool(matches), "resubmitted": False}
    if action == "collect_artifacts":
        return collect_child_artifacts(req, state, arguments)
    if action == "collect":
        return collect(req, state, identifier)
    if action == "decide":
        item = state["assignments"][identifier]
        if item["status"] == "assigned" and arguments.get("decision") == "reject":
            worker = checked(req, "GET", "/sessions/" + item["session_id"])
            if worker.get("status") not in {"exited", "closed"}:
                raise ValueError("An active worker needs an explicit safe lifecycle decision first")
            item.update(
                status="awaiting_decision",
                delivery_failed=True,
                scientific_success=None,
                worker_status=worker.get("status"),
                evidence_hashes={},
                rejection_scope="Explicit rejection without validated delivery; experiment outcome unknown",
            )
        if item["status"] != "awaiting_decision" or arguments.get("decision") not in {
            "adopt",
            "reject",
            "follow_up",
        }:
            raise ValueError("Collect verified evidence before deciding")
        if item.get("delivery_failed") and arguments["decision"] == "adopt":
            raise ValueError("Cannot adopt a worker with unavailable delivery")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 4000:
            raise ValueError("A bounded decision reason is required")
        item.update(status="decided", decision=arguments["decision"], reason=reason)
        save(state)
        return item
    raise ValueError("Unknown delegation action")


def should_retire(req, task=None):
    if task is not None:
        return False
    own = checked(req, "GET", "/sessions/self")
    return own.get("has_been_main") is True and own.get("is_main") is False


def guard_gateway(req, method, path, body):
    method = method.upper()
    path = urlsplit(path).path.rstrip("/")
    if method == "POST" and path != "/sessions/self/stop" and should_retire(req):
        raise PermissionError("Former coordinator must save state and exit after handoff")
    adopting = (
        method == "POST"
        and path.startswith("/evolution/trials/")
        and path.endswith("/decision")
        and (body or {}).get("retain")
    )
    if not (method == "POST" and path == "/jobs") and not adopting:
        return
    if not is_main(req):
        return
    if not adopting:
        raise PermissionError("Coordinator must delegate Policy and Harness execution to workers")
    target = path.split("/")[3]
    sessions = checked(req, "GET", "/sessions?include_status=true").get("sessions")
    if not isinstance(sessions, list):
        raise TypeError("Unknown worker state; preserve existing ownership")
    active = []
    for record in sessions:
        if record.get("session_id") == target:
            continue
        status = record.get("status")
        status = status.get("status") if isinstance(status, dict) else status
        if status not in {"exited", "closed"}:
            active.append(record.get("session_id"))
    if active:
        raise ValueError("Settle existing workers before handoff; do not cancel or orphan them")


def worker_task(argv):
    if "--worker-task" not in argv:
        return None
    position = argv.index("--worker-task") + 1
    if position >= len(argv) or argv[position] != "worker-task.json":
        raise ValueError("Worker task must be its own local worker-task.json")
    task = json.loads(pathlib.Path("worker-task.json").read_text())
    if task.get("version") != 1 or task.get("role") not in {"policy", "harness"}:
        raise ValueError("Invalid worker task; do not run main task instead")
    if not isinstance(task.get("deliverables"), list) or not isinstance(task.get("id"), str):
        raise TypeError("Invalid worker delivery contract")
    return task


def worker_delivery_status(task):
    """Explain local delivery validation without changing reports or acceptance."""
    if task is None:
        return {"ready": False, "status": "not_worker"}
    if not pathlib.Path("worker-result.json").is_file():
        return {"ready": False, "status": "missing", "reason": "No worker-result.json yet"}
    try:
        report = json.loads(pathlib.Path("worker-result.json").read_text())
        if not isinstance(report, dict):
            raise TypeError("Worker result must be a JSON object")
        if (
            report.get("id") != task["id"]
            or report.get("role") != task["role"]
            or report.get("status") not in {"complete", "blocked"}
        ):
            raise ValueError("Report identity, role or terminal status does not match this task")
        evidence = report.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 16:
            raise ValueError("Use at most 16 local evidence references")
        hashes = {}
        for item in evidence:
            try:
                name = relative_path(item["path"])
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(
                    "Evidence must be workspace-relative; remote /outputs needs authorized "
                    "artifact transfer to local files or a sealed, granted export"
                ) from error
            raw = text_file(name).encode()
            if digest(raw) != item.get("sha256"):
                raise ValueError("Evidence hash mismatch for " + name[:200])
            hashes[name] = item["sha256"]
        if report["status"] == "complete" and not set(task["deliverables"]) <= hashes.keys():
            raise ValueError("Complete report must include every declared deliverable path")
        return {"ready": True, "status": report["status"], "evidence_count": len(hashes)}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"ready": False, "status": "invalid", "reason": str(error)[:1000]}


def worker_result_ready(task):
    return worker_delivery_status(task)["ready"]


def entry_command(task):
    return ["python", "agent.py", "--resume"] + (
        ["--worker-task", "worker-task.json"] if task else []
    )


def decision_instructions(task):
    if task:
        return WORKER_INSTRUCTIONS.format(role=task["role"])
    return (
        "You are the main A3 research coordinator when Host main identity is yours. "
        "Set direction, assign Policy and Harness workers, coordinate dependencies, "
        "retrieve and assess evidence, decide and integrate tested results. Do not "
        "personally implement research or submit training/evaluation jobs. Simple "
        "coordination, result inspection and evidence decisions need no extra worker. "
        "Use existing permissions and frozen protocol; review independent advice "
        "and adopt/reject/defer with reasons. Candidate validation remains its bounded "
        "test assignment until actual adoption. Never reveal credentials."
    )


def install_input_skills(input_root=pathlib.Path("/run/input")):
    """Materialize selected deployed references, without injecting their whole text.

    The frozen manifest, not arbitrary catalog paths, defines eligible source bytes.
    Existing version directories remain immutable; publish the index only last.
    """
    input_root = pathlib.Path(input_root)
    manifest_path = input_root / "input-manifest.json"
    if not manifest_path.is_file():
        return
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("status") != "active" or not isinstance(manifest.get("files"), dict):
        raise ValueError("Reference inputs are not a stable active snapshot")
    candidates = [
        n
        for n in manifest["files"]
        if n.startswith("skills/") and n.endswith("/assets/catalog-entry.json")
    ]
    if not candidates:
        return
    index_path = pathlib.Path("skills/index.json")
    previous = index_path.read_bytes() if index_path.is_file() else None
    catalog = json.loads(previous) if previous is not None else {"version": 1, "skills": []}
    if not isinstance(catalog.get("skills"), list):
        raise TypeError("Preserve and reconcile invalid skill catalog")
    entries = {e["id"]: e for e in catalog["skills"]}
    for name in candidates:
        relative_path(name)
        raw = (input_root / name).read_bytes()
        if digest(raw) != manifest["files"][name]["sha256"]:
            raise ValueError("Reference catalog does not match frozen source")
        entry = json.loads(raw)
        identifier, version = entry.get("id"), entry.get("version")
        if entry.get("status") != "reference":
            continue  # Deployment metadata never authorizes executable skills.
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identifier)
            or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)
        ):
            raise ValueError("Invalid deployed reference identity")
        prefix = "skills/" + identifier + "/"
        if (
            name != prefix + "assets/catalog-entry.json"
            or entry.get("local_source") != prefix + "SKILL.md"
        ):
            raise ValueError("Reference identity/source mismatch")
        names = [n for n in manifest["files"] if n.startswith(prefix)]
        if len(names) > 128:
            raise ValueError("Reference tree exceeds bound")
        tree_hash = digest(
            json.dumps({n: manifest["files"][n]["sha256"] for n in names}, sort_keys=True).encode()
        )
        if any(e.get("deployed_tree_sha256") == tree_hash for e in entries.values()):
            continue  # A resume must not overwrite an Agent-improved skill copy.
        installed_id = (
            identifier
            if identifier not in entries
            else identifier + "-reference-" + version + "-" + tree_hash[:8]
        )
        destination = "skills/" + installed_id + "-reference-" + version + "/"
        source_hash = None
        for source in names:
            relative_path(source)
            original = input_root / source
            if original.is_symlink() or any(p.is_symlink() for p in original.parents):
                raise ValueError("Reference inputs cannot be symlinks")
            content = original.read_bytes()
            if len(content) > 1024 * 1024 or digest(content) != manifest["files"][source]["sha256"]:
                raise ValueError("Reference input changed or exceeded bound")
            target = pathlib.Path(destination + source[len(prefix) :])
            if target.is_file() and target.read_bytes() != content:
                raise ValueError("Existing reference version differs; preserve and reconcile")
            if not target.exists():
                context_manager.atomic(target, content.decode("utf-8"))
            if source == prefix + "SKILL.md":
                source_hash = digest(content)
        if source_hash is None:
            raise ValueError("Reference source missing")
        entries[installed_id] = {
            **entry,
            "id": installed_id,
            "reference_id": identifier,
            "deployed_tree_sha256": tree_hash,
            "local_source": destination + "SKILL.md",
            "source_sha256": source_hash,
            "evidence": [p.replace(prefix, destination, 1) for p in entry.get("evidence", [])],
            "external_input_revision": manifest.get("revision"),
        }
    if (
        manifest_path.read_bytes() != manifest_raw
        or (index_path.read_bytes() if index_path.exists() else None) != previous
    ):
        raise ValueError("Concurrent input/catalog change; index not published")
    updated = {**catalog, "skills": list(entries.values())}
    encoded = json.dumps(updated, ensure_ascii=False, indent=2)
    if previous is None or json.loads(previous) != updated:
        context_manager.atomic(index_path, encoded)
