"""Worker coordination: contracts."""

import json
import pathlib
import re

from a3_research_agent.harness.context import manager as context_manager
from a3_research_agent.harness.orchestration.common import digest

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
