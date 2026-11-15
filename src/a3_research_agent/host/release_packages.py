"""Freeze and verify packages for explicit maintenance of a suspended run."""

import hashlib
import json
import re
from pathlib import Path

from a3_research_agent.harness import state_paths
from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.inputs.versions import InputVersions, write_atomic
from a3_research_agent.host.published_harness import COMMAND, native_source, project_for_run
from a3_research_agent.path_safety import workspace_path


def encoded(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True).encode()


def prepare_release(
    root: Path,
    handoff: Path,
    *,
    commit: str,
    run: Path,
) -> dict:
    """Freeze a published source pin, research prompts, and reviewed release note."""
    native_source(root, commit)
    note = HumanFeedback.validate_content(handoff.read_bytes())
    if not re.fullmatch(r"\s*## 已核对结论\n\s*\S[\s\S]*?\n## 待办\n\s*\S[\s\S]*", note):
        raise ValueError("Handoff must contain nonempty 已核对结论 and 待办 sections")
    manifest = {
        "version": 1,
        "command": COMMAND.copy(),
        "files": [state_paths.CHECKPOINT],
        "harness_commit": commit,
    }
    package_files = {state_paths.CHECKPOINT: "[]", "run.json": encoded(manifest).decode()}
    inputs = InputVersions(root, run).prepare_update()["contents"]
    inputs["initial-feedback.md"] = (
        "# 研究维护交接\n\nVersion: 0.2.0\n\n"
        "以下是经操作者核对的研究状态。工具、接口和权限以当前部署为准。\n\n" + note
    )
    contents = {"package": package_files, "inputs": inputs}
    return {**contents, "digest": hashlib.sha256(encoded(contents)).hexdigest()}


def release_directory(anchor: Path) -> Path:
    """Keep explicit maintenance packages outside the research workspace."""
    return project_for_run(anchor) / ".runtime/releases" / anchor.name


def publish_release(anchor: Path, proposal: dict, confirm: str) -> None:
    if confirm != proposal["digest"]:
        raise ValueError("Release digest changed; preview again")
    state = release_directory(anchor)
    directory = state / "packages" / confirm
    contents = {name: proposal[name] for name in ("package", "inputs")}
    if hashlib.sha256(encoded(contents)).hexdigest() != confirm:
        raise ValueError("Release contents changed after preview")
    for kind, files in contents.items():
        for name, text in files.items():
            write_atomic(directory / kind / name, text.encode())
    write_atomic(
        directory / "manifest.json",
        encoded(
            {
                kind: {
                    name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()
                }
                for kind, files in contents.items()
            }
        ),
    )


def verify_release(directory: Path, digest: str) -> None:
    manifest = json.loads((directory / "manifest.json").read_text())
    contents = {
        kind: {name: workspace_path(directory, f"{kind}/{name}").read_text() for name in entries}
        for kind, entries in manifest.items()
    }
    if hashlib.sha256(encoded(contents)).hexdigest() != digest:
        raise ValueError("Published release content no longer matches its digest")
    for kind, entries in contents.items():
        if any(
            hashlib.sha256(text.encode()).hexdigest() != manifest[kind][name]
            for name, text in entries.items()
        ):
            raise ValueError("Published release file differs from its manifest hash")
        files = {
            p.relative_to(directory / kind).as_posix()
            for p in (directory / kind).rglob("*")
            if p.is_file()
        }
        if files != set(entries):
            raise ValueError("Unexpected files in the published release")
