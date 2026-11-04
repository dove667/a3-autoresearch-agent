"""Task preparation services used by Host launch."""

import hashlib
import json
from pathlib import Path

from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.inputs.versions import (
    EXTENSION_PROFILES,
    resolve_phase,
    resolve_task_profile,
)
from a3_research_agent.host.runs.continuation import (
    prepare_fresh_task,
)


def input_hashes(inputs: Path) -> dict[str, str]:
    names = (
        "goal.md",
        "acceptance.md",
        "bootstrap.yaml",
        "execution.md",
        "phase-instructions.md",
        "protocol.md",
    )
    return {
        name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
        for name in names
        if (inputs / name).is_file()
    }


def validate_skills_source(start) -> None:
    """Require Host-recorded E success, not a copied Agent outcome template."""
    source = start.previous_run
    try:
        if resolve_task_profile(source) != "web-extension":
            raise ValueError("Source must be a web-extension run")
        retained = json.loads((source / "retained.json").read_text())
        outcome = json.loads((source / "outcome.json").read_text())
        manifest = outcome.get("input_manifest", {})
        if (
            retained.get("status") != "ready"
            or retained.get("version_id") != start.source_version
            or outcome.get("status") != "complete"
            or manifest.get("phase") != "phase-one"
            or manifest.get("task_profile") != "web-extension"
            or not isinstance(manifest.get("acceptance_checks"), list)
            or "E" not in manifest["acceptance_checks"]
            or outcome.get("acceptance", {}).get("E", {}).get("status") != "passed"
        ):
            raise ValueError("Missing successful Host E acceptance or ready retained version")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError(
            f"skills-extension requires a successful retained E source: {error}"
        ) from error


def prepare_custom_task(start, goal: str, feedback: str | None = None):
    """Start a new task with retained capabilities, never inherited completion state."""
    fresh, context = prepare_fresh_task(start, goal)
    source = str(start.previous_run.resolve()) if start.previous_run else None
    identity = json.dumps([source, start.source_version, start.files], sort_keys=True)
    prefix = "inherited-task-state/" + hashlib.sha256(identity.encode()).hexdigest() + "/"
    updates = {
        "checkpoint.json": fresh.files["checkpoint.json"],
        "task.txt": goal,
        "trajectory.jsonl": "",
        "progress.md": "# New task\n\nCurrent acceptance is pending; inherited evidence is historical.\n",
        "run-outcome.json": json.dumps(
            {"status": "continue", "reason": "New task requires new evidence"}
        ),
        # Keep explicit retained file manifests valid without reusing old feedback.
        "initial-feedback.md": feedback or "",
    }
    context["initialized_task_files"] = {}
    for name, content in updates.items():
        if name in start.files:
            archive = prefix + name
            if any(
                path == archive or path.startswith(archive + "/") or archive.startswith(path + "/")
                for path in fresh.files
            ):
                raise ValueError(f"Inherited task archive collision: {archive}")
            fresh.files[archive] = start.files[name]
            context["initialized_task_files"][name] = {
                "source_sha256": hashlib.sha256(start.files[name].encode()).hexdigest(),
                "archive_path": archive,
                "source_run": source,
                "source_version": start.source_version,
            }
        fresh.files[name] = content
    context["inherited_acceptance"] = {
        "status": "historical-not-recertified",
        "source_run": source,
        "source_version": start.source_version,
    }
    if feedback is not None:
        data = feedback.encode("utf-8")
        HumanFeedback.validate_content(data)
        digest = hashlib.sha256(data).hexdigest()
        messages = json.loads(fresh.files["checkpoint.json"])
        messages.append(
            {"role": "user", "content": f"Initial human feedback (sha256: {digest})\n\n{feedback}"}
        )
        fresh.files["checkpoint.json"] = json.dumps(messages, ensure_ascii=False)
        context["initial_message_count"] = len(messages)
        context["initial_feedback"] = {
            "sha256": digest,
            "bytes": len(data),
            "file": "initial-feedback.md",
            "mode": "initial-user-input",
        }
    return fresh, context


def prepare_task_start(
    start,
    phase: str,
    task_profile: str,
    goal: str,
    *,
    initial_feedback: str | None = None,
    initial_feedback_source: Path | None = None,
    custom_inputs: bool = False,
):
    """Initialize copied task state; retain source snapshots and historical evidence."""
    if custom_inputs:
        return prepare_custom_task(start, goal, initial_feedback)
    if initial_feedback is not None and (
        phase != "phase-one" or task_profile not in EXTENSION_PROFILES
    ):
        raise ValueError("Initial feedback requires a new phase-one extension task")
    if task_profile in EXTENSION_PROFILES:
        if phase != "phase-one" or not start.previous_run or not start.source_version:
            raise ValueError(f"{task_profile} requires a phase-one retained run source")
        if resolve_phase(start.previous_run) != "phase-one":
            raise ValueError(f"{task_profile} requires a phase-one retained run source")
        if task_profile == "skills-extension":
            validate_skills_source(start)
        check = "F" if task_profile == "skills-extension" else "E"
        historical = "A-E" if check == "F" else "A-D"
        fresh, context = prepare_fresh_task(start, goal)
        context["inherited_acceptance"] = {
            "checks": ["A", "B", "C", "D", "E"] if check == "F" else ["A", "B", "C", "D"],
            "status": "historical-not-recertified",
            "source_run": str(start.previous_run.resolve()),
            "source_version": start.source_version,
        }
        initial_state = {
            "task.txt": goal,
            "trajectory.jsonl": "",
            "progress.md": f"# Fresh {task_profile} task\n\n{check} is pending; historical {historical} is not recertified.\n",
            "run-outcome.json": json.dumps(
                {
                    "status": "continue",
                    "reason": f"Fresh {task_profile} task requires new {check} evidence and outcome",
                }
            ),
        }
        if task_profile == "skills-extension":
            if initial_feedback is None and "initial-feedback.md" in fresh.files:
                for name in ("resume.json", "continuation.json"):
                    if name in fresh.files:
                        declaration = json.loads(fresh.files[name])
                        if "initial-feedback.md" in declaration.get("files", []):
                            raise ValueError(
                                "Archiving initial-feedback.md would invalidate explicit manifest "
                                f"{name}; refusing to produce a dangling file reference"
                            )
            initial_state["initial-feedback.md"] = None
        context["initialized_task_files"] = {}
        for name, current in initial_state.items():
            if name not in fresh.files:
                continue
            archive_root = (
                "inherited-task-state/skills-extension/"
                if task_profile == "skills-extension"
                else "inherited-task-state/"
            )
            archive = archive_root + name
            if any(
                path == archive or path.startswith(archive + "/") or archive.startswith(path + "/")
                for path in fresh.files
            ):
                raise ValueError(f"Inherited task archive collision: {archive}")
            previous = fresh.files[name]
            context["initialized_task_files"][name] = {
                "source_sha256": hashlib.sha256(previous.encode()).hexdigest(),
                "archive_path": archive,
                "source_run": str(start.previous_run.resolve()),
                "source_version": start.source_version,
            }
            fresh.files[archive] = previous
            if current is None:
                del fresh.files[name]
            else:
                fresh.files[name] = current
        if "run-outcome.json" in context["initialized_task_files"]:
            terminal = context["initialized_task_files"]["run-outcome.json"]
            context["retired_terminal"] = {
                "file": "run-outcome.json",
                "sha256": terminal["source_sha256"],
                "source_run": terminal["source_run"],
                "source_version": terminal["source_version"],
            }
        if initial_feedback is not None:
            content = initial_feedback.encode("utf-8")
            HumanFeedback.validate_content(content)
            if any(
                path == "initial-feedback.md" or path.startswith("initial-feedback.md/")
                for path in fresh.files
            ):
                raise ValueError("Initial feedback file collision")
            sha256 = hashlib.sha256(content).hexdigest()
            messages = json.loads(fresh.files["checkpoint.json"])
            messages.append(
                {
                    "role": "user",
                    "content": f"Initial human feedback (sha256: {sha256})\n\n{initial_feedback}",
                }
            )
            fresh.files["checkpoint.json"] = json.dumps(messages, ensure_ascii=False)
            fresh.files["initial-feedback.md"] = initial_feedback
            context["initial_message_count"] = len(messages)
            context["initial_feedback"] = {
                "sha256": sha256,
                "source": str(initial_feedback_source.resolve())
                if initial_feedback_source
                else None,
                "bytes": len(content),
                "file": "initial-feedback.md",
                "mode": "initial-user-input",
            }
        return fresh, context
    if phase == "phase-two":
        return prepare_fresh_task(start, goal)
    return start, None
