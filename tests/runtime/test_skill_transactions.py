"""Skill package edits recover from catchable and abrupt process interruption."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from a3_research_agent.harness import skill_library, skill_registry

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
CRASH = """
import json, os, sys
from pathlib import Path
from a3_research_agent.harness import skill_library, skill_registry
root, history = map(Path, sys.argv[1:3])
arguments = json.loads(sys.argv[3])
write = skill_library.atomic_write
catalog = root / 'method/assets/catalog-entry.json'
def interrupted(path, data):
    if path == catalog:
        if sys.argv[4] == 'after':
            write(path, data)
        os._exit(17)
    write(path, data)
skill_library.atomic_write = interrupted
skill_registry.dispatch(arguments, root=root, history_root=history)
"""


def call(root, action, **arguments):
    return skill_registry.dispatch({"action": action, **arguments}, root=root)


def package_state(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def prepare(root, action):
    call(root, "list")
    if action != "new":
        call(
            root,
            "save",
            id="method",
            description="Original",
            content="original body",
            source="fixture",
        )
        attachment = call(
            root,
            "write_resource",
            id="method",
            path="references/usage.md",
            content="original attachment",
        )
        body = call(root, "read", id="method")
        if action == "restore":
            call(root, "archive", id="method", expected_sha256=body["sha256"])
            body = call(root, "read", id="method")
    if action in {"new", "save", "restore"}:
        return {
            "action": "save",
            "id": "method",
            "description": "Changed",
            "content": "new body",
            "source": "fixture",
            **({"expected_sha256": body["sha256"]} if action != "new" else {}),
        }
    if action == "archive":
        return {"action": "archive", "id": "method", "expected_sha256": body["sha256"]}
    if action == "delete":
        return {
            "action": "delete_resource",
            "id": "method",
            "path": "references/usage.md",
            "expected_sha256": attachment["sha256"],
        }
    return {
        "action": "write_resource",
        "id": "method",
        "path": "references/new.md" if action == "create-resource" else "references/usage.md",
        "content": "new attachment",
        **({"expected_sha256": attachment["sha256"]} if action == "replace-resource" else {}),
    }


def crash(root, history, arguments, phase="before"):
    result = subprocess.run(
        [sys.executable, "-c", CRASH, str(root), str(history), json.dumps(arguments), phase],
        check=False,
        env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 17, result.stderr
    assert (history / ".pending.json").is_file()


@pytest.mark.parametrize(
    "action", ["new", "save", "archive", "restore", "create-resource", "replace-resource", "delete"]
)
def test_next_tool_call_recovers_an_abruptly_interrupted_edit(tmp_path, monkeypatch, action):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    arguments = prepare(root, action)
    before = package_state(root)
    crash(root, history, arguments)
    assert "error" not in call(root, "list")
    assert not (history / ".pending.json").exists()
    assert package_state(root) == before


def test_fully_published_package_is_kept_when_interrupted_before_journal_cleanup(
    tmp_path, monkeypatch
):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    arguments = prepare(root, "save")
    previous = call(root, "read", id="method")
    crash(root, history, arguments, "after")
    current = call(root, "read", id="method")
    assert "error" not in current and current["content"].endswith("new body")
    assert current["version"] != previous["version"]
    assert not (history / ".pending.json").exists()


def test_keyboard_interrupt_restores_the_package_and_propagates(tmp_path, monkeypatch):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    arguments = prepare(root, "save")
    before = package_state(root)
    write = skill_library.atomic_write
    interrupted = False

    def stop(path, data):
        nonlocal interrupted
        if path == root / "method/assets/catalog-entry.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        write(path, data)

    monkeypatch.setattr(skill_library, "atomic_write", stop)
    with pytest.raises(KeyboardInterrupt):
        skill_registry.dispatch(arguments, root=root)
    assert package_state(root) == before
    assert not (history / ".pending.json").exists()


def test_changed_recovery_snapshot_is_not_used_to_overwrite_the_library(tmp_path, monkeypatch):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    crash(root, history, prepare(root, "save"))
    pending = json.loads((history / ".pending.json").read_text())
    archived = history / pending["previous_revision"] / "SKILL.md"
    archived.write_text("corrupted archive")
    interrupted_body = (root / "method/SKILL.md").read_bytes()
    assert call(root, "list")["error"] == "skill_history_changed"
    assert (root / "method/SKILL.md").read_bytes() == interrupted_body
    assert (history / ".pending.json").exists()


def test_unexpected_edits_after_interruption_are_preserved_for_review(tmp_path, monkeypatch):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    crash(root, history, prepare(root, "save"))
    external = root / "method/references/external.md"
    external.write_text("Concurrent human edit")
    assert call(root, "list")["error"] == "skill_transaction_conflict"
    assert external.read_text() == "Concurrent human edit"
    assert (history / ".pending.json").exists()


@pytest.mark.parametrize(
    "unicode_paths", [False, True], ids=["maximum-ascii-path", "unicode-json-escapes"]
)
def test_large_legal_package_journal_remains_recoverable(tmp_path, monkeypatch, unicode_paths):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    arguments = prepare(root, "save")
    for index in range(skill_library.MAX_FILES - 3):
        if unicode_paths:
            relative = "/".join(["🧪" * 40] * 3 + [f"{index:03d}" + "🧪" * 30])
        else:
            relative = f"{index:03d}/" + "a" * 169 + "/" + "b" * 169 + "/" + "c" * 168
            assert len(relative) == skill_library.MAX_PATH_CHARS
        path = root / "method" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("evidence")
    assert len(skill_library.package_files(root, "method")) == skill_library.MAX_FILES
    before = package_state(root)
    crash(root, history, arguments)
    assert (history / ".pending.json").stat().st_size > 128 * 1024
    assert "error" not in call(root, "list")
    assert not (history / ".pending.json").exists()
    assert package_state(root) == before


def test_unrelated_missing_attachment_is_not_silently_restored(tmp_path, monkeypatch):
    root, history = tmp_path / "skills", tmp_path / "history"
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(history))
    crash(root, history, prepare(root, "save"))
    unchanged_attachment = root / "method/references/usage.md"
    unchanged_attachment.unlink()
    assert call(root, "list")["error"] == "skill_transaction_conflict"
    assert not unchanged_attachment.exists()
    assert (history / ".pending.json").exists()
