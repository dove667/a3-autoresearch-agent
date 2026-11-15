"""Initialize a new native research task from versioned inputs and retained knowledge."""

import hashlib
import json
from pathlib import Path

from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.runs.continuation import prepare_fresh_task
from a3_research_agent.workspace.paths import workspace_path


def input_hashes(inputs: Path) -> dict[str, str]:
    manifest = json.loads(workspace_path(inputs, "input-manifest.json").read_text())
    if manifest.get("status") != "active" or not isinstance(manifest.get("files"), dict):
        raise ValueError("Task inputs require an active versioned manifest")
    hashes = {}
    for name, record in manifest["files"].items():
        digest = hashlib.sha256(workspace_path(inputs, name).read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise ValueError("Task input differs from its active manifest")
        hashes[name] = digest
    return hashes


def prepare_custom_task(start, goal: str, feedback: str | None = None):
    """Create fresh model/task state while carrying retained research files forward."""
    fresh, context = prepare_fresh_task(start, goal)
    fresh.files.update(
        {
            "task.txt": goal,
            "trajectory.jsonl": "",
            "progress.md": "# Research progress\n",
            "run-outcome.json": json.dumps({"status": "continue", "reason": "New research task"}),
        }
    )
    context["source_run"] = str(start.previous_run.resolve()) if start.previous_run else None
    if feedback is not None:
        data = feedback.encode("utf-8")
        HumanFeedback.validate_content(data)
        digest = hashlib.sha256(data).hexdigest()
        messages = json.loads(fresh.files["checkpoint.json"])
        messages.append(
            {
                "role": "user",
                "content": f"Initial human feedback (sha256: {digest})\n\n{feedback}",
            }
        )
        fresh.files["checkpoint.json"] = json.dumps(messages, ensure_ascii=False)
        fresh.files["initial-feedback.md"] = feedback
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
    goal: str,
    *,
    initial_feedback: str | None = None,
    initial_feedback_source: Path | None = None,
):
    """Start the published runtime with an explicit current research task."""
    fresh, context = prepare_custom_task(start, goal, initial_feedback)
    if initial_feedback_source and "initial_feedback" in context:
        context["initial_feedback"]["source"] = str(initial_feedback_source.resolve())
    return fresh, context
