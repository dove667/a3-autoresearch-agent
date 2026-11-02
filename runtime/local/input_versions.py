"""Publish versioned Agent inputs and preserve the contents used by each run."""

import hashlib
import json
import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, TemplateError

SHARED_INPUT_SOURCES = {
    "bootstrap.yaml": "configs/rsi-bootstrap.yaml",
    "execution.md": "runtime/local/README.md",
    "readiness-task.md": "runtime/local/readiness-task.md",
    "workspace-instructions.md": "configs/prompts/workspace.md",
}
PHASES = ("phase-one", "phase-two")
TASK_PROFILES = ("standard", "web-extension")


def input_sources(phase: str, task_profile: str = "standard") -> dict[str, str]:
    if phase not in PHASES:
        raise ValueError(f"Unknown phase: {phase}")
    if task_profile not in TASK_PROFILES:
        raise ValueError(f"Unknown task profile: {task_profile}")
    if task_profile == "web-extension" and phase != "phase-one":
        raise ValueError("web-extension requires phase-one")
    bundle = "phase-one-web" if task_profile == "web-extension" else phase
    sources = {
        **SHARED_INPUT_SOURCES,
        "goal.md": f"evals/{bundle}/goal.md",
        "acceptance.md": f"evals/{bundle}/acceptance.md",
        "phase-instructions.md": f"configs/prompts/{bundle}.md",
    }
    if phase == "phase-two":
        sources["protocol.md"] = "evals/phase-two/protocol.md"
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
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


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
        run: Path,
        phase: str | None = None,
        task_profile: str | None = None,
    ):
        self.source_root = source_root
        self.run = run
        self.phase = resolve_phase(run, phase)
        self.task_profile = resolve_task_profile(run, task_profile)
        self.sources = input_sources(self.phase, self.task_profile)
        self.lock = threading.RLock()
        self.manifest: dict = {}
        self.contents: dict[str, str] = {}

    def prepare(self) -> dict:
        """Read a proposed update without changing the active run."""
        with self.lock:
            contents, files = {}, {}
            for name, relative in self.sources.items():
                data = (self.source_root / relative).read_bytes()
                text = data.decode("utf-8")
                match = VERSION.search(text)
                if not match:
                    raise ValueError(f"Missing semantic version in {relative}")
                contents[name] = text
                files[name] = {
                    "source": relative,
                    "version": ".".join(match.groups()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            for previous in (self.run / "input-revisions").glob("*/manifest.json"):
                saved = json.loads(previous.read_text())
                if saved.get("status") != "active":
                    continue
                for name, item in files.items():
                    old = saved["files"].get(name, {})
                    if (
                        old.get("version") == item["version"]
                        and old.get("sha256") != item["sha256"]
                    ):
                        raise ValueError(f"{name} changed without a version increment")
            validate_bootstrap(contents["bootstrap.yaml"])
            digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            return {
                "phase": self.phase,
                "task_profile": self.task_profile,
                "digest": digest,
                "files": files,
                "contents": contents,
            }

    def activate(self, proposal: dict, event: str) -> dict:
        """Publish an already selected bundle; callers own update authorization."""
        with self.lock:
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
                "task_profile": self.task_profile,
                "revision": directory.name,
                "digest": proposal["digest"],
                "files": proposal["files"],
                "event": event,
                "time": datetime.now().astimezone().isoformat(),
                "status": "active",
                "generation": index,
                "acceptance_checks": re.findall(
                    r"^## ([A-Z])\.", proposal["contents"]["acceptance.md"], re.MULTILINE
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
            return tuple(re.findall(r"^## ([A-Z])\.", self.contents["acceptance.md"], re.MULTILINE))
