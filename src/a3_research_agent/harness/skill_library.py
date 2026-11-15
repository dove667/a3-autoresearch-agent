"""Read and version the single project skill tree behind the skills tool."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import yaml

MAX_BODY_BYTES = 128 * 1024
MAX_RESOURCE_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_FILES = 128
MAX_PATH_CHARS = 512
# JSON may encode one Unicode character as a twelve-byte surrogate pair.
MAX_PENDING_BYTES = 2 * MAX_FILES * (12 * MAX_PATH_CHARS + 96) + 64 * 1024
INTERNAL_DIRECTORIES = {"revisions", "__pycache__"}
CATALOG = "assets/catalog-entry.json"
BODY = "SKILL.md"


class SkillError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value):
        raise SkillError("invalid_skill_id")
    return value


def checked_path(root, relative):
    if not isinstance(relative, str):
        raise SkillError("invalid_skill_path")
    path = PurePosixPath(relative)
    if (
        path.as_posix() != relative
        or path.is_absolute()
        or "\\" in relative
        or not path.parts
        or len(relative) > MAX_PATH_CHARS
        or any(part.startswith(".") or part in INTERNAL_DIRECTORIES for part in path.parts)
    ):
        raise SkillError("invalid_skill_path")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise SkillError("symlink_rejected")
    return target


def resource_path(root, skill_id, relative):
    directory = root / identifier(skill_id)
    path = checked_path(directory, relative)
    if relative.casefold() in {BODY.casefold(), CATALOG.casefold()}:
        raise SkillError("managed_skill_file")
    current = directory
    for part in PurePosixPath(relative).parts:
        if current.is_dir():
            requested = current / part
            exists = requested.exists()
            aliases = [
                child.name
                for child in current.iterdir()
                if child.name.casefold() == part.casefold()
                or (exists and child.samefile(requested))
            ]
            if len(aliases) > 1 or (aliases and part != aliases[0]):
                raise SkillError("skill_path_alias")
        current = current / part
    return path


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".skill-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked_library(root, history_root):
    if root.is_symlink():
        raise SkillError("symlink_rejected")
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".registry.lock"
    if lock.is_symlink():
        raise SkillError("symlink_rejected")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        recover_pending(root, history_root)
        yield


def read_catalog(root, skill_id):
    directory = checked_path(root, identifier(skill_id))
    if not directory.exists():
        return None
    path = checked_path(directory, CATALOG)
    if not path.is_file() or path.stat().st_size > 16384:
        raise SkillError("invalid_skill_catalog")
    entry = json.loads(path.read_text())
    if (
        not isinstance(entry, dict)
        or entry.get("id") != skill_id
        or entry.get("local_source") != f"skills/{skill_id}/{BODY}"
        or not isinstance(entry.get("description"), str)
        or not isinstance(entry.get("keywords", []), list)
        or any(not isinstance(word, str) for word in entry.get("keywords", []))
        or not isinstance(entry.get("version"), str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", entry["version"])
    ):
        raise SkillError("invalid_skill_catalog")
    return entry


def catalogs(root):
    entries = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.name in INTERNAL_DIRECTORIES or not path.is_dir():
            continue
        if len(entries) >= 2000:
            raise SkillError("skill_library_too_large")
        entries.append(read_catalog(root, identifier(path.name)))
    return entries


def read_bytes(path, maximum):
    if not path.is_file() or path.stat().st_size > maximum:
        raise SkillError("missing_or_oversized_resource")
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise SkillError("missing_or_oversized_resource")
    return raw


def frontmatter(content):
    if not content.startswith("---\n"):
        return {}, content
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        raise SkillError("invalid_skill_frontmatter")
    try:
        header = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError as error:
        raise SkillError("invalid_skill_frontmatter") from error
    if not isinstance(header, dict) or not isinstance(header.get("metadata", {}), dict):
        raise SkillError("invalid_skill_frontmatter")
    return header, parts[1].lstrip("\n")


def body_bytes(content, entry):
    header, body = frontmatter(content)
    header["name"] = entry["id"]
    header.setdefault("description", entry["description"])
    header["metadata"] = {**header.get("metadata", {}), "version": entry["version"]}
    encoded = (
        "---\n" + yaml.safe_dump(header, allow_unicode=True, sort_keys=False) + "---\n\n" + body
    ).encode()
    if len(encoded) > MAX_BODY_BYTES:
        raise SkillError("skill_too_large")
    return encoded


def read_body(root, entry):
    raw = read_bytes(checked_path(root / entry["id"], BODY), MAX_BODY_BYTES)
    header, _ = frontmatter(raw.decode("utf-8"))
    if (
        header.get("name") != entry["id"]
        or header.get("metadata", {}).get("version") != entry["version"]
    ):
        raise SkillError("skill_metadata_mismatch")
    return raw


def package_files(root, skill_id):
    directory = checked_path(root, identifier(skill_id))
    files, total = {}, 0
    for current, directories, names in os.walk(directory, followlinks=False):
        current = Path(current)
        for name in directories + names:
            if (current / name).is_symlink():
                raise SkillError("symlink_rejected")
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and name not in INTERNAL_DIRECTORIES
        )
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = current / name
            raw = read_bytes(path, MAX_RESOURCE_BYTES)
            total += len(raw)
            files[path.relative_to(directory).as_posix()] = raw
            if len(files) > MAX_FILES or total > MAX_PACKAGE_BYTES:
                raise SkillError("skill_package_too_large")
    return files


def next_version(version):
    major, minor, patch = map(int, version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def snapshot(history_root, skill_id, entry, files):
    hashes = {name: digest(raw) for name, raw in files.items()}
    identity = digest(json.dumps(hashes, sort_keys=True).encode())
    directory = checked_path(history_root, f"{skill_id}/{entry['version']}-{identity}")
    for name, raw in files.items():
        target = checked_path(directory, name)
        if target.exists() and target.read_bytes() != raw:
            raise SkillError("skill_history_changed")
        if not target.exists():
            atomic_write(target, raw)
    manifest = {"id": skill_id, "version": entry["version"], "files": hashes}
    atomic_write(directory / ".snapshot.json", json.dumps(manifest, sort_keys=True).encode())
    return directory


def file_hashes(files):
    return {name: digest(raw) for name, raw in files.items()}


def pending_path(history_root):
    path = history_root / ".pending.json"
    if path.is_symlink():
        raise SkillError("symlink_rejected")
    return path


def recover_pending(root, history_root):
    """Finish a fully published edit, or restore its verified previous package."""
    pending = pending_path(history_root)
    if not pending.exists():
        return
    journal = json.loads(read_bytes(pending, MAX_PENDING_BYTES))
    if (
        not isinstance(journal, dict)
        or journal.get("library_root") != str(root)
        or not isinstance(journal.get("old"), dict)
        or not isinstance(journal.get("new"), dict)
    ):
        raise SkillError("invalid_skill_transaction")
    skill_id = identifier(journal.get("id"))
    directory = checked_path(root, skill_id)
    actual = file_hashes(package_files(root, skill_id)) if directory.exists() else {}
    for table in (journal["old"], journal["new"]):
        if len(table) > MAX_FILES:
            raise SkillError("invalid_skill_transaction")
        for name, checksum in table.items():
            checked_path(directory, name)
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise SkillError("invalid_skill_transaction")
    if actual == journal["new"]:
        pending.unlink()
        return
    missing_unchanged = (journal["old"].keys() & journal["new"].keys()) - actual.keys()
    if missing_unchanged:
        raise SkillError("skill_transaction_conflict")
    if any(
        checksum not in {journal["old"].get(name), journal["new"].get(name)}
        for name, checksum in actual.items()
    ):
        raise SkillError("skill_transaction_conflict")
    previous = {}
    if journal["old"]:
        archived = checked_path(history_root, journal["previous_revision"])
        previous = {
            name: read_bytes(checked_path(archived, name), MAX_RESOURCE_BYTES)
            for name in journal["old"]
        }
        if file_hashes(previous) != journal["old"]:
            raise SkillError("skill_history_changed")
    for name, raw in previous.items():
        if name != CATALOG and actual.get(name) != digest(raw):
            atomic_write(checked_path(directory, name), raw)
    for name in actual.keys() - previous.keys():
        checked_path(directory, name).unlink()
    if CATALOG in previous and actual.get(CATALOG) != digest(previous[CATALOG]):
        atomic_write(checked_path(directory, CATALOG), previous[CATALOG])
    if directory.exists():
        for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and path.name.startswith(".skill-"):
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if not previous:
            directory.rmdir()
    pending.unlink()


def commit_package(root, history_root, entry, body, changes=None):
    """Journal one package edit so process interruption cannot split its version."""
    skill_id = entry["id"]
    directory = checked_path(root, skill_id)
    previous = read_catalog(root, skill_id)
    old = package_files(root, skill_id) if previous else {}
    updated = {**old, **(changes or {}), BODY: body}
    updated[CATALOG] = (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode()
    updated = {name: raw for name, raw in updated.items() if raw is not None}
    if len(updated) > MAX_FILES or sum(map(len, updated.values())) > MAX_PACKAGE_BYTES:
        raise SkillError("skill_package_too_large")
    revision = snapshot(history_root, skill_id, previous, old) if previous else None
    relative_revision = revision.relative_to(history_root).as_posix() if revision else None
    journal = {
        "library_root": str(root),
        "id": skill_id,
        "old": file_hashes(old),
        "new": file_hashes(updated),
        "previous_revision": relative_revision,
    }
    pending = pending_path(history_root)
    encoded_journal = json.dumps(journal, sort_keys=True).encode()
    if len(encoded_journal) > MAX_PENDING_BYTES:
        raise SkillError("skill_transaction_too_large")
    atomic_write(pending, encoded_journal)
    try:
        directory.mkdir(exist_ok=True)
        for name, raw in updated.items():
            if name != CATALOG:
                atomic_write(checked_path(directory, name), raw)
        for name in old.keys() - updated.keys():
            checked_path(directory, name).unlink()
        atomic_write(checked_path(directory, CATALOG), updated[CATALOG])
        pending.unlink()
    except BaseException:
        recover_pending(root, history_root)
        raise
    return relative_revision
