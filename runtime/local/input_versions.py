"""Publish versioned Agent inputs and preserve the contents used by each run."""

import hashlib
import json
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, TemplateError

SHARED_INPUT_SOURCES = {
    "SOUL.md": "agent-inputs/SOUL.md",
    "bootstrap.yaml": "agent-inputs/shared/bootstrap.yaml",
    "execution.md": "agent-inputs/shared/execution.md",
    "readiness-task.md": "agent-inputs/shared/readiness-task.md",
    "workspace-instructions.md": "agent-inputs/shared/workspace.md",
}
PHASES = ("phase-one", "phase-two")
TASK_PROFILES = ("standard", "web-extension", "skills-extension")
EXTENSION_PROFILES = ("web-extension", "skills-extension")


def input_sources(phase: str, task_profile: str = "standard") -> dict[str, str]:
    if phase not in PHASES:
        raise ValueError(f"Unknown phase: {phase}")
    if task_profile not in TASK_PROFILES:
        raise ValueError(f"Unknown task profile: {task_profile}")
    if task_profile in EXTENSION_PROFILES and phase != "phase-one":
        raise ValueError(f"{task_profile} requires phase-one")
    bundle = {"web-extension": "phase-one-web", "skills-extension": "phase-one-skills"}.get(
        task_profile, phase
    )
    sources = {
        **SHARED_INPUT_SOURCES,
        "goal.md": f"agent-inputs/{bundle}/goal.md",
        "acceptance.md": f"agent-inputs/{bundle}/acceptance.md",
        "phase-instructions.md": f"agent-inputs/{bundle}/instructions.md",
    }
    if phase == "phase-two":
        sources["protocol.md"] = "agent-inputs/phase-two/protocol.md"
    return sources


def resolve_phase(run: Path | None, requested: str | None = None) -> str:
    """Keep a run in its recorded stage; older runs belong to phase one."""
    recorded = []
    paths = (run / "startup.json", run / "inputs/input-manifest.json") if run else ()
    for path in paths:
        if path.is_file():
            saved = json.loads(path.read_text())
            phase = saved.get("phase") or saved.get("input_manifest", {}).get("phase")
            recorded.append(phase or "phase-one")
    phase = requested or (recorded[0] if recorded else "phase-one")
    if phase not in PHASES:
        raise ValueError(f"Unknown phase: {phase}")
    if any(previous != phase for previous in recorded):
        raise ValueError("Cannot change phase within a run; start a new run with --run-harness")
    return phase


def resolve_task_profile(run: Path | None, requested: str | None = None) -> str:
    """A profile is immutable within a run; pre-profile runs are standard."""
    recorded = []
    paths = (run / "startup.json", run / "inputs/input-manifest.json") if run else ()
    for path in paths:
        if path.is_file():
            saved = json.loads(path.read_text())
            profile = saved.get("task_profile") or saved.get("input_manifest", {}).get(
                "task_profile"
            )
            recorded.append(profile or "standard")
    profile = requested or (recorded[0] if recorded else "standard")
    if profile not in TASK_PROFILES:
        raise ValueError(f"Unknown task profile: {profile}")
    if any(previous != profile for previous in recorded):
        raise ValueError(
            "Cannot change task profile within a run; start a new run with --run-harness"
        )
    return profile


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


def validate_bootstrap(text: str) -> None:
    try:
        config = yaml.safe_load(text)
        if not isinstance(config, dict):
            raise TypeError("Expected a mapping")
        agent, run = config.get("agent"), config.get("run")
        if not isinstance(agent, dict) or not isinstance(run, dict):
            raise TypeError("Missing agent or run configuration")
        for name in ("system_template", "instance_template"):
            if not isinstance(agent.get(name), str):
                raise TypeError(f"Missing text template: {name}")
            Environment().parse(agent[name])
        if not isinstance(run.get("task"), str):
            raise TypeError("Missing text task")
    except (yaml.YAMLError, TemplateError, TypeError) as error:
        raise ValueError(f"Invalid bootstrap.yaml: {error}") from None


class InputVersions:
    def __init__(
        self,
        source_root: Path,
        run: Path | None,
        phase: str | None = None,
        task_profile: str | None = None,
        custom_dir: Path | None = None,
    ):
        self.source_root = source_root
        self.run = run
        self.phase = resolve_phase(run, phase)
        self.task_profile = resolve_task_profile(run, task_profile)
        self.sources = input_sources(self.phase, self.task_profile)
        self.input_mode = "custom" if custom_dir else "preset"
        self.custom_dir = custom_dir
        self.frozen_manifest = None
        saved_path = run / "inputs/input-manifest.json" if run else None
        if saved_path and saved_path.is_file():
            saved = json.loads(saved_path.read_text())
            if saved.get("input_mode") == "custom":
                if custom_dir is not None:
                    raise ValueError("Cannot replace custom inputs on resume; start a new run")
                revision = saved.get("revision", "")
                if not re.fullmatch(r"revision-\d{4,}", revision):
                    raise ValueError("Invalid recorded custom input revision")
                self.input_mode = "custom"
                self.custom_dir = run / "input-revisions" / revision
                self.frozen_manifest = saved
        if self.input_mode == "custom":
            if self.task_profile != "standard":
                raise ValueError("Custom inputs cannot be combined with a preset task profile")
            self.sources = self._custom_sources()
        self.lock = threading.RLock()
        self.manifest: dict = {}
        self.contents: dict[str, str] = {}

    @classmethod
    def from_proposal(cls, source_root: Path, run: Path, proposal: dict):
        """Use the already validated bytes, not a second read of mutable source files."""
        instance = cls(source_root, run, proposal["phase"], proposal["task_profile"])
        instance.input_mode = proposal.get("input_mode", "preset")
        instance.sources = {name: item["source"] for name, item in proposal["files"].items()}
        return instance

    def _custom_sources(self) -> dict[str, str]:
        directory = self.custom_dir
        if not directory or not directory.is_dir() or directory.is_symlink():
            raise ValueError("Agent input directory must be a real directory")
        if self.frozen_manifest:
            names = list(self.frozen_manifest["files"])
        else:
            names = []
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Agent inputs cannot contain symlinks: {path.name}")
                if path.is_file():
                    names.append(path.relative_to(directory).as_posix())
                elif not path.is_dir():
                    raise ValueError(f"Unsupported Agent input: {path.name}")
        required = set(input_sources(self.phase))
        missing = required - set(names)
        if missing:
            raise ValueError(
                f"Incomplete Agent input directory; missing: {', '.join(sorted(missing))}"
            )
        reserved = {
            "input-manifest.json",
            "manifest.json",
            "deployment.json",
            "previous-run.json",
            "history",
            "common-instructions.md",
        }
        for name in names:
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] in reserved
            ):
                raise ValueError(f"Reserved or unsafe Agent input path: {name}")
        return {name: name for name in names}

    def prepare(self) -> dict:
        """Read a proposed update without changing the active run."""
        with self.lock:
            contents, files = {}, {}
            for name, relative in self.sources.items():
                path = (
                    (self.custom_dir / name)
                    if self.input_mode == "custom"
                    else (self.source_root / relative)
                )
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or (
                        self.input_mode == "custom"
                        and any(
                            (self.custom_dir / parent).is_symlink()
                            for parent in path.relative_to(self.custom_dir).parents
                            if parent != Path(".")
                        )
                    )
                ):
                    raise ValueError(f"Missing or unsafe input: {name}")
                data = path.read_bytes()
                text = data.decode("utf-8")
                match = VERSION.search(text)
                if not match and (self.input_mode != "custom" or name in input_sources(self.phase)):
                    raise ValueError(f"Missing semantic version in {relative}")
                contents[name] = text
                files[name] = {
                    "source": str(self.custom_dir.resolve() / name)
                    if self.input_mode == "custom"
                    else relative,
                    "version": ".".join(match.groups()) if match else None,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                if self.frozen_manifest:
                    old = self.frozen_manifest["files"][name]
                    if old["sha256"] != files[name]["sha256"]:
                        raise ValueError(f"Recorded custom input snapshot changed: {name}")
                    files[name] = dict(old)
            previous_manifests = (
                (self.run / "input-revisions").glob("*/manifest.json") if self.run else ()
            )
            for previous in previous_manifests:
                saved = json.loads(previous.read_text())
                if saved.get("status") != "active":
                    continue
                for name, item in files.items():
                    old = saved["files"].get(name, {})
                    if (
                        item["version"] is not None
                        and old.get("version") == item["version"]
                        and old.get("sha256") != item["sha256"]
                    ):
                        raise ValueError(f"{name} changed without a version increment")
            validate_bootstrap(contents["bootstrap.yaml"])
            if self.input_mode == "custom":
                checks = re.findall(
                    r"^## ([A-Za-z][A-Za-z0-9_-]*)\.(?:\s|$)",
                    contents["acceptance.md"],
                    re.MULTILINE,
                )
                if not checks or len(checks) != len(set(checks)):
                    raise ValueError("Custom acceptance needs unique ## check_id. headings")
            digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            return {
                "phase": self.phase,
                "input_mode": self.input_mode,
                "task_profile": self.task_profile,
                "digest": digest,
                "files": files,
                "contents": contents,
            }

    def activate(self, proposal: dict, event: str) -> dict:
        """Publish an already selected bundle; callers own update authorization."""
        with self.lock:
            if self.run is None:
                raise ValueError("Input preview has no run directory to activate")
            if proposal.get("input_mode", "preset") != self.input_mode:
                raise ValueError("Cannot change input mode within a run")
            if proposal["phase"] != self.phase:
                raise ValueError("Cannot activate another phase within a run")
            if proposal.get("task_profile", "standard") != self.task_profile:
                raise ValueError("Cannot activate another task profile within a run")
            revisions = self.run / "input-revisions"
            revisions.mkdir(parents=True, exist_ok=True)
            legacy = revisions / "legacy"
            if not legacy.exists() and (self.run / "inputs").exists():
                legacy.mkdir()
                for name in (*self.sources, "common-instructions.md", "input-manifest.json"):
                    original = self.run / "inputs" / name
                    if original.is_file():
                        write_atomic(legacy / name, original.read_bytes())
            index = 1
            while True:
                directory = revisions / f"revision-{index:04d}"
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    index += 1
            manifest = {
                "phase": self.phase,
                "input_mode": self.input_mode,
                "task_profile": self.task_profile,
                "revision": directory.name,
                "digest": proposal["digest"],
                "files": proposal["files"],
                "event": event,
                "time": datetime.now().astimezone().isoformat(),
                "status": "active",
                "generation": index,
                "acceptance_checks": re.findall(
                    r"^## ([A-Za-z][A-Za-z0-9_-]*)\.(?:\s|$)",
                    proposal["contents"]["acceptance.md"],
                    re.MULTILINE,
                ),
            }
            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            targets = [self.run / "inputs"]
            if (self.run / "private-inputs").is_dir():
                targets.append(self.run / "private-inputs")
            replacements = {}
            for name, text in proposal["contents"].items():
                data = text.encode()
                write_atomic(directory / name, data)
                replacements.update({target / name: data for target in targets})
            # Retire the old mounted prompt after archiving it; new requests use one stage prompt.
            for target in targets:
                old_prompt = target / "common-instructions.md"
                if old_prompt.exists():
                    replacements[old_prompt] = None
            workspace_guide = self.run / "workspace/harness/AGENTS.md"
            if workspace_guide.parent.exists():
                replacements[workspace_guide] = proposal["contents"][
                    "workspace-instructions.md"
                ].encode()
            replacements.update(
                {target / "input-manifest.json": manifest_bytes for target in targets}
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
            if self.input_mode == "custom":
                self.custom_dir = directory
                self.frozen_manifest = deepcopy(manifest)
            return deepcopy(manifest)

    def model_instructions(self) -> tuple[dict, str]:
        with self.lock:
            manifest = deepcopy(self.manifest)
            instructions = (
                f"当前 Agent 输入：{manifest['revision']}，内容标识 {manifest['digest']}。\n"
                f"当前阶段：{self.phase}；任务配置：{self.task_profile}。目标、验收和阶段协议以以下当前版本为准；"
                "历史上下文中的旧版内容保留供追溯。\n"
                "执行接口与其他说明见 /run/input/input-manifest.json 所列当前文件；"
                "连续读取多个输入文件时，前后必须都是status=active且revision与generation相同；"
                "status=updating时等待，标识变化后重新读取。\n\n"
                + "SOUL 是跨阶段长期角色原则；SOUL 不得覆盖当前阶段的 goal、acceptance、protocol 或权限边界。\n"
                + self.contents["SOUL.md"]
                + "\n\n"
                + self.contents["phase-instructions.md"]
                + "\n\n"
                + self.contents["goal.md"]
                + "\n\n"
                + self.contents["acceptance.md"]
                + ("\n\n" + self.contents["protocol.md"] if "protocol.md" in self.contents else "")
            )
            return manifest, instructions

    def acceptance_checks(self) -> tuple[str, ...]:
        with self.lock:
            return tuple(
                re.findall(
                    r"^## ([A-Za-z][A-Za-z0-9_-]*)\.(?:\s|$)",
                    self.contents["acceptance.md"],
                    re.MULTILINE,
                )
            )
