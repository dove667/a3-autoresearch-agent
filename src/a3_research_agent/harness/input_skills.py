"""Install versioned deployed skill references into the single research workspace."""

import hashlib
import json
import pathlib
import re

from a3_research_agent.harness import paths
from a3_research_agent.harness.context import manager as context_manager


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def relative_path(name):
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path == pathlib.PurePosixPath("."):
        raise ValueError("Unsafe reference path")
    return path.as_posix()


def unedited_deployed_reference(entry, identifier):
    """Only replace a deployed reference whose whole installed tree is unchanged."""
    if (
        not entry
        or entry.get("status") != "reference"
        or entry.get("reference_id") != identifier
        or not entry.get("deployed_tree_sha256")
        or entry.get("managed_by") == "skills"
    ):
        return False
    name = entry.get("local_source")
    if not isinstance(name, str) or not name.startswith("skills/"):
        return False
    source = pathlib.Path(relative_path(name))
    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
        return False
    try:
        if digest(source.read_bytes()) != entry.get("source_sha256"):
            return False
        files = {}
        for path in source.parent.rglob("*"):
            if path.is_symlink():
                return False
            if path.is_file():
                if path.stat().st_size > 1024 * 1024 or len(files) >= 128:
                    return False
                relative = path.relative_to(source.parent).as_posix()
                files[f"skills/{identifier}/{relative}"] = digest(path.read_bytes())
        return digest(json.dumps(files, sort_keys=True).encode()) == entry["deployed_tree_sha256"]
    except OSError:
        return False


def available_reference_id(identifier, version, tree_hash, entries):
    """Allocate an identifiable reference ID without replacing any catalog history."""
    base = identifier + "-reference-" + version + "-" + tree_hash[:8]
    candidate, sequence = base, 2
    while candidate in entries:
        candidate = f"{base}-{sequence}"
        sequence += 1
    return candidate


def install_input_skills(input_root=None):
    """Materialize selected deployed references, without injecting their whole text.

    The frozen manifest, not arbitrary catalog paths, defines eligible source bytes.
    Existing version directories remain immutable; publish the index only last.
    """
    input_root = pathlib.Path(input_root) if input_root is not None else paths.input_root()
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
        matching = [
            e
            for e in entries.values()
            if e.get("status") == "reference" and e.get("deployed_tree_sha256") == tree_hash
        ]
        current = entries.get(identifier)
        replaceable = unedited_deployed_reference(current, identifier)
        if matching and (not replaceable or current.get("deployed_tree_sha256") == tree_hash):
            continue  # A resume must not overwrite an Agent-improved skill copy.
        if replaceable:
            archived_id = available_reference_id(
                identifier, current["version"], current["deployed_tree_sha256"], entries
            )
            entries[archived_id] = {**current, "id": archived_id, "status": "archived"}
            installed_id = identifier
            replacement = next(
                (e for e in matching if unedited_deployed_reference(e, identifier)), None
            )
            if replacement is not None:
                # Repair catalogs that already installed the new tree under a separate ID.
                del entries[replacement["id"]]
                entries[identifier] = {**replacement, "id": identifier}
                continue
        elif current is None:
            installed_id = identifier
        else:
            installed_id = available_reference_id(identifier, version, tree_hash, entries)
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
