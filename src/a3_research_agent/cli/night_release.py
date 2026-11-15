"""Freeze operator-reviewed Harness releases and select the next night launch."""

import argparse
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
        "# 夜间研究交接\n\nVersion: 0.1.0\n\n"
        "以下是经操作者核对的研究状态。工具、接口和权限以当前部署为准。\n\n" + note
    )
    contents = {"package": package_files, "inputs": inputs}
    return {**contents, "digest": hashlib.sha256(encoded(contents)).hexdigest()}


def night_state_directory(anchor: Path) -> Path:
    """Keep scheduler state separate from the persistent research workspace."""
    return project_for_run(anchor) / ".runtime/night-window" / anchor.name


def publish_release(anchor: Path, proposal: dict, confirm: str) -> None:
    if confirm != proposal["digest"]:
        raise ValueError("Release digest changed; preview again")
    state = night_state_directory(anchor)
    directory = state / "night-releases" / confirm
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
    write_atomic(state / "night-release.json", encoded({"digest": confirm}))


def read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class NightSchedule:
    """Resume published releases in the same research workspace."""

    def __init__(self, anchor: Path, resume_command: list[str]):
        self.anchor = anchor
        self.command = resume_command
        self.directory = night_state_directory(anchor)
        self.state = self.directory / "night-schedule.json"
        self.resume_index = resume_command.index("--resume-from")

    def current_run(self) -> Path:
        state = read_json(self.state, {"current": self.anchor.name})
        return workspace_path(self.anchor.parent, state["current"])

    def select(self) -> tuple[list[str], Path, dict[str, str]]:
        state = read_json(self.state, {"current": self.anchor.name})
        current = workspace_path(self.anchor.parent, state["current"])
        release = read_json(self.directory / "night-release.json", {}).get("digest")
        if not release or release == state.get("release"):
            command = (
                self.command[: self.resume_index + 2]
                if state.get("release")
                else self.command.copy()
            )
            command[self.resume_index + 1] = str(current)
            return command, current, {}

        digest = release
        directory = self.directory / "night-releases" / digest
        self.verify(directory, digest)
        package = json.loads((directory / "package/run.json").read_text())
        if not package.get("harness_commit"):
            raise ValueError("Release requires a published Harness commit")
        from a3_research_agent.host.releases import apply_release

        apply_release(current, directory, digest)
        write_atomic(self.state, encoded({"current": current.name, "release": digest}))
        command = self.command[: self.resume_index + 2]
        command[self.resume_index + 1] = str(current)
        return command, current, {}

    @staticmethod
    def verify(directory: Path, digest: str) -> None:
        manifest = json.loads((directory / "manifest.json").read_text())
        contents = {
            kind: {
                name: workspace_path(directory, f"{kind}/{name}").read_text() for name in entries
            }
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


def main() -> None:
    from datetime import datetime

    from a3_research_agent.cli.night_window import BEIJING, window_boundary

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("anchor", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    anchor = args.anchor.resolve()
    try:
        root = project_for_run(anchor)
    except ValueError as error:
        parser.error(str(error))
    if anchor.parent != root / "trajectories" or not (
        (anchor / "startup.json").is_file()
        or (night_state_directory(anchor) / "night-schedule.json").is_file()
    ):
        parser.error("Select an existing night-window anchor under trajectories")
    try:
        state = read_json(
            night_state_directory(anchor) / "night-schedule.json", {"current": anchor.name}
        )
        current = workspace_path(anchor.parent, state["current"])
        proposal = prepare_release(
            root,
            args.handoff,
            commit=args.commit,
            run=current,
        )
    except (OSError, ValueError, SyntaxError) as error:
        parser.error(str(error))
    print("Command:", json.loads(proposal["package"]["run.json"])["command"])
    print("Files:", ", ".join(sorted(proposal["package"])))
    print("Handoff:", proposal["inputs"]["initial-feedback.md"])
    print("Release digest:", proposal["digest"])
    if args.confirm:
        if window_boundary(datetime.now(BEIJING))[0]:
            parser.error("Publish during the daytime pause")
        publish_release(anchor, proposal, args.confirm)
        print("Published for the next night window")


if __name__ == "__main__":
    main()
