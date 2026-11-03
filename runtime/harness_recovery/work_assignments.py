"""Bounded worker packages and durable coordination, using existing Host permissions."""

import base64
import hashlib
import json
import pathlib
import re
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
    "entries with path and sha256, and exports listing ready IDs granted to current main. "
    "Use only these report fields, summary at most 4000 characters; put detailed logs in evidence files. Include every "
    "declared deliverable. A report is a claim for the coordinator to assess, not proof "
    "of adoption. Save recoverable job/asset references before exiting. Do not start "
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


def worker_package(arguments):
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
    if len(files) > 128 or sum(len(s.encode()) for s in files.values()) > 20 * 1024 * 1024:
        raise ValueError("Worker package exceeds existing Host upload bound")
    return task, files


def artifact(req, session, name):
    result = checked(req, "GET", "/sessions/" + session + "/artifacts?" + urlencode({"path": name}))
    raw = base64.b64decode(result["data"], validate=True)
    if len(raw) > 1024 * 1024:
        raise ValueError("Worker report too large; use bounded reports and sealed exports")
    return raw


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
    for reference in evidence:
        name = relative_path(reference["path"])
        raw = artifact(req, session, name)
        if reference.get("sha256") != digest(raw):
            raise ValueError("Worker evidence hash mismatch; no adoption decision recorded")
        target = pathlib.Path("orchestration/results") / identifier / name
        context_manager.atomic(target, raw.decode("utf-8"))
        hashes[name] = digest(raw)
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
        exports=exports,
    )
    save(state)
    return item


def view():
    state = load()
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


def dispatch(req, arguments):
    if not is_main(req):
        raise PermissionError("Only the current coordinator assigns and decides worker work")
    state = load()
    action = arguments.get("action")
    if action == "list":
        return {
            **view(),
            "assignments": [
                {
                    k: item.get(k)
                    for k in ("id", "role", "status", "session_id", "decision", "reason")
                }
                for item in state["assignments"].values()
            ],
        }
    identifier = arguments.get("id")
    if action == "assign":
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
        task, files = worker_package(arguments)
        item = {
            "id": identifier,
            "role": task["role"],
            "status": "creation_uncertain",
            "deliverables": task["deliverables"],
            "source_hashes": task["source_hashes"],
            "task_sha256": digest(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()),
        }
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


def worker_result_ready(task):
    if task is None or not pathlib.Path("worker-result.json").is_file():
        return False
    try:
        report = json.loads(pathlib.Path("worker-result.json").read_text())
        if (
            report.get("id") != task["id"]
            or report.get("role") != task["role"]
            or report.get("status") not in {"complete", "blocked"}
        ):
            return False
        evidence = report.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 16:
            return False
        hashes = {}
        for item in evidence:
            name = relative_path(item["path"])
            raw = text_file(name).encode()
            if digest(raw) != item.get("sha256"):
                return False
            hashes[name] = item["sha256"]
        return report["status"] == "blocked" or set(task["deliverables"]) <= hashes.keys()
    except (OSError, ValueError, TypeError, KeyError):
        return False


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
