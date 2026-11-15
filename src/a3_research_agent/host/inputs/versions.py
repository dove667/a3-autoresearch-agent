"""Publish versioned Agent inputs and preserve the contents used by each run."""

import hashlib
import json
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

PROMPT_SOURCES = {
    "SOUL.md": "prompt/SOUL.md",
    "execution.md": "prompt/execution.md",
    "workspace-instructions.md": "prompt/workspace.md",
}


def deployment_input_sources(source_root: Path) -> dict[str, str]:
    """Select the current prompts and complete reviewed method packages."""
    sources = dict(PROMPT_SOURCES)
    root = source_root / "skills"
    if root.is_symlink():
        raise ValueError("Deployed skill references cannot be symlinks")
    if not root.is_dir():
        raise ValueError("Missing deployed skill reference directory")
    directories = []
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or directory.name == "__pycache__":
            continue
        if directory.is_symlink():
            raise ValueError("Deployed skill references cannot be symlinks")
        if directory.is_dir():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", directory.name):
                raise ValueError("Invalid deployed skill reference name")
            directories.append(directory)
    if not directories:
        raise ValueError("No deployed skill references")
    for directory in directories:
        identifier = directory.name
        selected = []
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(source_root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("Deployed skill references cannot be symlinks")
            if path.is_file() and path.suffix in {".md", ".json", ".py"}:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Deployed skill reference exceeds file size bound")
                selected.append(relative.as_posix())
        required = {
            f"skills/{identifier}/SKILL.md",
            f"skills/{identifier}/assets/catalog-entry.json",
        }
        if not required <= set(selected) or len(selected) > 128:
            raise ValueError("Deployed skill reference tree is incomplete or oversized")
        sources.update({name: name for name in selected})
    return sources


def skill_input_versions(contents: dict[str, str]) -> dict[str, str]:
    """Version a complete skill tree by its catalog, with separate per-file hashes."""
    versions = {}
    for name, text in contents.items():
        if not name.startswith("skills/") or not name.endswith("/assets/catalog-entry.json"):
            continue
        catalog = json.loads(text)
        if not isinstance(catalog, dict):
            raise TypeError("Skill reference catalog must be an object")
        prefix = name.removesuffix("assets/catalog-entry.json")
        identifier = prefix.split("/")[1]
        version = catalog.get("version")
        if (
            catalog.get("id") != identifier
            or catalog.get("local_source") != prefix + "SKILL.md"
            or not isinstance(version, str)
            or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
            or prefix + "SKILL.md" not in contents
        ):
            raise ValueError("Invalid versioned skill reference catalog")
        versions.update({path: version for path in contents if path.startswith(prefix)})
    return versions


def execution_index(text: str) -> dict:
    """Bounded H2 index in read-tool character units; the reference stays lossless."""
    headings = list(re.finditer(r"^## ([^\r\n]+)", text, re.MULTILINE))
    sections = []
    for index, match in enumerate(headings):
        section = {
            "title": match.group(1)[:80],
            "offset": match.start(),
            "end": headings[index + 1].start() if index + 1 < len(headings) else len(text),
        }
        if (
            len(sections) >= 64
            or len(json.dumps(sections + [section], ensure_ascii=False).encode()) > 6000
        ):
            break
        sections.append(section)
    return {
        "path": "execution.md",
        "unit": "characters",
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "total": len(text),
        "preamble_end": headings[0].start() if headings else len(text),
        "sections": sections,
        "truncated": len(sections) < len(headings),
        "next_offset": headings[len(sections)].start()
        if len(sections) < len(headings)
        else len(text),
    }


VERSION = re.compile(
    r"^(?:Version|version):\s*(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\s*$", re.MULTILINE
)


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".a3-input-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class InputVersions:
    def __init__(self, source_root: Path, run: Path | None):
        self.source_root, self.run = source_root, run
        self.snapshot_directory = None
        self.frozen_manifest = None
        self.provided = None
        self.lock = threading.RLock()
        self.manifest, self.contents = {}, {}
        saved = run / "inputs/input-manifest.json" if run else None
        if saved and saved.is_file():
            self.frozen_manifest = json.loads(saved.read_text())
            revision = self.frozen_manifest.get("revision", "")
            if not re.fullmatch(r"revision-\d{4,}", revision):
                raise ValueError("Invalid recorded input revision")
            self.snapshot_directory = run / "input-revisions" / revision
            self.sources = {name: name for name in self.frozen_manifest["files"]}
        else:
            if run and (run / "startup.json").is_file():
                raise ValueError("Recorded runs require their current input manifest")
            self.sources = deployment_input_sources(source_root)

    @classmethod
    def from_proposal(cls, source_root: Path, run: Path, proposal: dict):
        instance = cls.__new__(cls)
        instance.source_root, instance.run = source_root, run
        instance.snapshot_directory, instance.frozen_manifest = None, None
        instance.sources = {name: record["source"] for name, record in proposal["files"].items()}
        instance.provided = deepcopy(proposal)
        instance.lock = threading.RLock()
        instance.manifest, instance.contents = {}, {}
        return instance

    def prepare_update(self) -> dict:
        """Preview the repository's complete current prompts and method packages."""
        candidate = InputVersions(self.source_root, None)
        candidate.run = self.run
        return candidate.prepare()

    def prepare(self) -> dict:
        """Read a proposed update without changing the active run."""
        with self.lock:
            if self.provided is not None:
                selected = self.provided["contents"]
            elif self.frozen_manifest:
                selected = {}
                for name in self.sources:
                    path = self.snapshot_directory / name
                    if path.is_symlink() or any(
                        parent.is_symlink()
                        for parent in path.parents
                        if parent != self.snapshot_directory.parent
                    ):
                        raise ValueError(f"Unsafe recorded input: {name}")
                    selected[name] = path.read_bytes().decode("utf-8")
            else:
                self.sources = deployment_input_sources(self.source_root)
                selected = {}
                for name, source in self.sources.items():
                    path = Path(source) if Path(source).is_absolute() else self.source_root / source
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(f"Missing or unsafe input: {name}")
                    selected[name] = path.read_bytes().decode("utf-8")
            contents, files = {}, {}
            for name, relative in self.sources.items():
                text = selected[name]
                data = text.encode("utf-8")
                match = VERSION.search(text)
                if not match and name in PROMPT_SOURCES:
                    raise ValueError(f"Missing semantic version in {relative}")
                contents[name] = text
                files[name] = {
                    "source": relative,
                    "version": ".".join(match.groups()) if match else None,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                if self.frozen_manifest:
                    old = self.frozen_manifest["files"][name]
                    if old["sha256"] != files[name]["sha256"]:
                        raise ValueError(f"Recorded input snapshot changed: {name}")
                    files[name] = dict(old)
            for name, version in skill_input_versions(contents).items():
                if not self.frozen_manifest:
                    files[name]["version"] = version
            previous_manifests = (
                (self.run / "input-revisions").glob("*/manifest.json") if self.run else ()
            )
            for previous in previous_manifests:
                saved = json.loads(previous.read_text())
                if saved.get("status") != "active":
                    continue
                for name, item in files.items():
                    if not name.startswith("skills/") or not name.endswith(
                        "/assets/catalog-entry.json"
                    ):
                        continue
                    if saved["files"].get(name, {}).get("version") != item["version"]:
                        continue
                    prefix = name.removesuffix("assets/catalog-entry.json")
                    old_tree = {
                        path: record["sha256"]
                        for path, record in saved["files"].items()
                        if path.startswith(prefix)
                    }
                    new_tree = {
                        path: record["sha256"]
                        for path, record in files.items()
                        if path.startswith(prefix)
                    }
                    if old_tree != new_tree:
                        raise ValueError(f"{prefix} changed without a version increment")
                for name, item in files.items():
                    old = saved["files"].get(name, {})
                    if (
                        item["version"] is not None
                        and old.get("version") == item["version"]
                        and old.get("sha256") != item["sha256"]
                    ):
                        raise ValueError(f"{name} changed without a version increment")
            digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            return {
                "digest": digest,
                "files": files,
                "contents": contents,
            }

    def activate(self, proposal: dict, event: str) -> dict:
        """Publish an already selected bundle; callers own update authorization."""
        with self.lock:
            if self.run is None:
                raise ValueError("Input preview has no run directory to activate")
            revisions = self.run / "input-revisions"
            revisions.mkdir(parents=True, exist_ok=True)
            index = 1
            while True:
                directory = revisions / f"revision-{index:04d}"
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    index += 1
            manifest = {
                "revision": directory.name,
                "digest": proposal["digest"],
                "files": proposal["files"],
                "event": event,
                "time": datetime.now().astimezone().isoformat(),
                "status": "active",
                "generation": index,
                "execution_index": execution_index(proposal["contents"]["execution.md"]),
            }
            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            targets = [self.run / "inputs"]
            replacements = {}
            for name, text in proposal["contents"].items():
                data = text.encode()
                write_atomic(directory / name, data)
                replacements.update({target / name: data for target in targets})
            replacements.update(
                {target / "input-manifest.json": manifest_bytes for target in targets}
            )
            current_manifest = self.run / "inputs/input-manifest.json"
            if current_manifest.is_file():
                old_names = json.loads(current_manifest.read_text())["files"]
                replacements.update(
                    {
                        self.run / "inputs" / name: None
                        for name in old_names.keys() - proposal["contents"].keys()
                    }
                )
            backups = {path: path.read_bytes() if path.exists() else None for path in replacements}
            event_log = self.run / "input-events.jsonl"
            old_events = event_log.read_bytes() if event_log.exists() else b""
            replacements[event_log] = (
                old_events + (json.dumps(manifest, ensure_ascii=False) + "\n").encode()
            )
            backups[event_log] = old_events if event_log.exists() else None
            try:
                publishing = {**manifest, "status": "updating"}
                for target in targets:
                    write_atomic(target / "input-manifest.json", json.dumps(publishing).encode())
                for path, data in replacements.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, data)
                write_atomic(directory / "manifest.json", manifest_bytes)
            except OSError:
                for target in targets:
                    write_atomic(target / "input-manifest.json", json.dumps(publishing).encode())
                for path, data in backups.items():
                    if self.manifest and path.name == "input-manifest.json":
                        continue
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, data)
                if self.manifest:
                    rollback = {**self.manifest, "generation": index}
                    for target in targets:
                        write_atomic(
                            target / "input-manifest.json",
                            json.dumps(rollback, ensure_ascii=False, indent=2).encode(),
                        )
                    write_atomic(
                        event_log,
                        old_events
                        + (
                            json.dumps({**rollback, "event": "update_failed"}, ensure_ascii=False)
                            + "\n"
                        ).encode(),
                    )
                    self.manifest = rollback
                raise
            self.manifest = manifest
            self.contents = dict(proposal["contents"])
            self.snapshot_directory = directory
            self.frozen_manifest = deepcopy(manifest)
            self.provided = None
            self.sources = {name: name for name in manifest["files"]}
            return deepcopy(manifest)

    def model_instructions(self) -> tuple[dict, str]:
        with self.lock:
            manifest = deepcopy(self.manifest)
            instructions = (
                f"当前 Agent 输入：{manifest['revision']}，generation={manifest['generation']}，status={manifest['status']}，内容标识 {manifest['digest']}。\n"
                "SOUL 定义持续研究目标与行为原则。历史输入保留供追溯，当前运行使用本次版本。\n"
                "详细接口按需读取：A3_INPUT_ROOT 下 input-manifest.json 的 execution_index 给出 execution.md 章节字符 offset/end 和完整 SHA256，配合 read 的 fields 及分页；不要通读全部接口作为每轮前置步骤。"
                "本请求已给出当前输入标识；同一已验证 revision 的已读内容无需重复读取或每轮再查 manifest。"
                "连续读取多个输入文件时，前后必须都是status=active且revision与generation相同；"
                "status=updating时等待，标识变化后重新读取。\n\n" + self.contents["SOUL.md"]
            )
            return manifest, instructions
