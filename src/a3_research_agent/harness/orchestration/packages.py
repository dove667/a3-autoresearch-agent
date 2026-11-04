"""Worker coordination: packages."""

import json
import os
import pathlib
import re
import stat
import uuid

from a3_research_agent.harness.orchestration.common import digest
from a3_research_agent.harness.orchestration.contracts import (
    WORKER_INSTRUCTIONS,
    assignment_contract,
    relative_path,
    text_file,
)
from a3_research_agent.packaging.harness_payload import build_harness_payload

CORE_FILES = tuple(build_harness_payload()) + ("skill_tools.py", "web_tools.py")


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
