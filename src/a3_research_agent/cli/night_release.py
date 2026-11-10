"""Freeze operator-reviewed Harness releases and select the next night launch."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.inputs.versions import input_sources, write_atomic
from a3_research_agent.host.runs.directories import create_run_directory
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.packages import load_harness_package
from a3_research_agent.workspace.paths import workspace_path


def encoded(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True).encode()


def prepare_release(root: Path, package: Path, handoff: Path) -> dict:
    """Materialize the exact Python source and reviewed research note for approval."""
    command, files = load_harness_package(package)
    if not files or any(Path(name).suffix != ".py" for name in files):
        raise ValueError("Release manifests may contain only Python source")
    for name, source in files.items():
        if Path(name).as_posix() != name or any(part.startswith(".") for part in Path(name).parts):
            raise ValueError(f"Invalid release source path: {name}")
        compile(source, name, "exec")
    note = HumanFeedback.validate_content(handoff.read_bytes())
    if not re.fullmatch(r"\s*## 已核对结论\n\s*\S[\s\S]*?\n## 待办\n\s*\S[\s\S]*", note):
        raise ValueError("Handoff must contain nonempty 已核对结论 and 待办 sections")
    package_files = {
        **files,
        "checkpoint.json": "[]",
        "run.json": encoded(
            {"command": command, "files": sorted([*files, "checkpoint.json"])}
        ).decode(),
    }
    inputs = {
        name: (root / source).read_text() for name, source in input_sources("phase-two").items()
    }
    inputs["initial-feedback.md"] = (
        "# 夜间研究交接\n\nVersion: 0.1.0\n\n"
        "以下是经操作者核对的研究状态。工具、接口和权限以当前部署为准。\n\n" + note
    )
    contents = {"package": package_files, "inputs": inputs}
    return {**contents, "digest": hashlib.sha256(encoded(contents)).hexdigest()}


def publish_release(anchor: Path, proposal: dict, confirm: str) -> None:
    if confirm != proposal["digest"]:
        raise ValueError("Release digest changed; preview again")
    directory = anchor / ".host/night-releases" / confirm
    contents = {name: proposal[name] for name in ("package", "inputs")}
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
    write_atomic(anchor / ".host/night-release.json", encoded({"digest": confirm}))


def read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class NightSchedule:
    """Pin a new run before launching so a restart cannot fork the release twice."""

    def __init__(self, anchor: Path, resume_command: list[str]):
        self.anchor = anchor
        self.command = resume_command
        self.state = anchor / ".host/night-schedule.json"
        self.resume_index = resume_command.index("--resume-from")

    def select(self) -> tuple[list[str], Path, dict[str, str]]:
        state = read_json(self.state, {"current": self.anchor.name})
        current = self.anchor.parent / state["current"]
        target_name = state.get("target")
        if target_name:
            target = self.anchor.parent / target_name
            if (target / "startup.json").is_file():
                if RunStateStore(target).load().get("status") != "suspended":
                    raise RuntimeError(f"Released run requires recovery: {target}")
                state = {"current": target.name, "release": state["target_release"]}
                write_atomic(self.state, encoded(state))
                current = target
                target_name = None
            elif not target.is_dir() or any(target.iterdir()):
                raise RuntimeError(f"Interrupted release requires inspection: {target}")

        release = read_json(self.anchor / ".host/night-release.json", {}).get("digest")
        if not target_name and (not release or release == state.get("release")):
            command = (
                self.command[: self.resume_index + 2]
                if state.get("release")
                else self.command.copy()
            )
            command[self.resume_index + 1] = str(current)
            return command, current, {}

        digest = state["target_release"] if target_name else release
        directory = self.anchor / ".host/night-releases" / digest
        self.verify(directory, digest)
        if not target_name:
            if RunStateStore(current).load().get("status") != "suspended":
                raise RuntimeError(f"Pause the current run before starting the release: {current}")
            target = create_run_directory(self.anchor.parent)
            state.update(target=target.name, target_release=digest)
            write_atomic(self.state, encoded(state))
        command = [
            *self.command[: self.resume_index],
            "--phase",
            "phase-two",
            "--run-harness",
            str(directory / "package"),
            "--agent-inputs-dir",
            str(directory / "inputs"),
        ]
        return command, target, {"A3_NIGHT_WINDOW_TARGET": str(target)}

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
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    anchor = args.anchor.resolve()
    if anchor.parent != root / "artifacts/rsi" or not (anchor / "startup.json").is_file():
        parser.error("Select an existing night-window anchor under artifacts/rsi")
    try:
        proposal = prepare_release(root, args.package, args.handoff)
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
