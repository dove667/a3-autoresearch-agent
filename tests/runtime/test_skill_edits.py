"""Small text edits preserve skill metadata, revisions and complete packages."""

from pathlib import Path

import pytest
import yaml

from a3_research_agent.harness import skill_library as library
from a3_research_agent.harness import skill_registry as skills


def call(root, action, **arguments):
    return skills.dispatch({"action": action, **arguments}, root=root)


def create(root, body="# Method\nCheck inputs.\nKeep records."):
    return call(
        root, "save", id="method", description="Input verification", source="trial-1", content=body
    )


def package(root):
    return library.package_files(root, "method")


def test_edit_changes_only_markdown_and_snapshots_the_complete_previous_package(tmp_path):
    root = tmp_path / "skills"
    saved = create(root)
    before = package(root)
    result = call(
        root,
        "edit",
        id="method",
        expected_sha256=saved["sha256"],
        old_text="Check inputs.",
        new_text="Compare the actual input tensors.",
    )
    assert result["version"] == "0.1.1" and result["replacements"] == 1
    current = call(root, "read", id="method")
    assert current["sha256"] == result["sha256"]
    assert "Compare the actual input tensors." in current["content"]
    header = yaml.safe_load(current["content"].split("---", 2)[1])
    assert header["name"] == "method" and header["description"] == "Input verification"
    assert header["metadata"]["version"] == "0.1.1"
    history = root.parent / ".runtime/skill-revisions" / result["history_revision"]
    assert {name: (history / name).read_bytes() for name in before} == before


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"old_text": "description: Input verification"}, "edit_text_not_found"),
        ({"old_text": "absent"}, "edit_text_not_found"),
        ({"old_text": ""}, "edit_requires_old_and_new_text"),
        ({"replace_all": "yes"}, "invalid_replace_all"),
        ({"expected_sha256": None}, "revision_conflict"),
        ({"expected_sha256": "0" * 64}, "revision_conflict"),
    ],
)
def test_invalid_edits_leave_the_package_unchanged(tmp_path, changes, error):
    root = tmp_path / "skills"
    saved = create(root)
    before = package(root)
    result = call(
        root,
        "edit",
        id="method",
        **{
            "expected_sha256": saved["sha256"],
            "old_text": "Check inputs.",
            "new_text": "Changed",
            **changes,
        },
    )
    assert result["error"] == error
    assert package(root) == before
    assert not list((tmp_path / ".runtime/skill-revisions").rglob(".snapshot.json"))


def test_ambiguous_edit_requires_explicit_replace_all_and_noop_keeps_version(tmp_path):
    root = tmp_path / "skills"
    saved = create(root, "Compare inputs.\nCompare inputs.")
    before = package(root)
    arguments = {
        "id": "method",
        "expected_sha256": saved["sha256"],
        "old_text": "Compare inputs.",
        "new_text": "Compare samples.",
    }
    assert call(root, "edit", **arguments)["error"] == "edit_text_ambiguous"
    assert package(root) == before
    result = call(root, "edit", **arguments, replace_all=True)
    assert result["replacements"] == 2
    assert call(root, "read", id="method")["content"].count("Compare samples.") == 2
    same = call(
        root,
        "edit",
        id="method",
        expected_sha256=result["sha256"],
        old_text="Compare samples.",
        new_text="Compare samples.",
        replace_all=True,
    )
    assert same["unchanged"] and same["version"] == result["version"]


def test_resource_edit_uses_resource_hash_and_keeps_other_resources(tmp_path):
    root = tmp_path / "skills"
    create(root)
    saved = call(
        root,
        "write_resource",
        id="method",
        path="references/inputs.md",
        content="Inspect raw samples.",
    )
    call(
        root,
        "write_resource",
        id="method",
        path="references/unrelated.md",
        content="Keep this file.",
    )
    before = package(root)
    result = call(
        root,
        "edit_resource",
        id="method",
        path="references/inputs.md",
        expected_sha256=saved["sha256"],
        old_text="raw",
        new_text="paired",
    )
    assert result["version"] == "0.1.3" and result["replacements"] == 1
    assert result["sha256"] != saved["sha256"]
    assert (
        call(root, "read_resource", id="method", path="references/inputs.md")["content"]
        == "Inspect paired samples."
    )
    assert package(root)["references/unrelated.md"] == before["references/unrelated.md"]
    assert result["skill_sha256"] == call(root, "read", id="method")["sha256"]
    assert (
        call(
            root,
            "edit_resource",
            id="method",
            path="SKILL.md",
            old_text="Method",
            new_text="Other",
            expected_sha256=result["skill_sha256"],
        )["error"]
        == "managed_skill_file"
    )


def test_binary_and_oversized_edits_are_rejected_without_changes(tmp_path):
    root = tmp_path / "skills"
    saved = create(root)
    result = call(
        root,
        "edit",
        id="method",
        expected_sha256=saved["sha256"],
        old_text="Check inputs.",
        new_text="x" * library.MAX_BODY_BYTES,
    )
    assert result["error"] == "skill_too_large"
    binary = root / "method" / "binary.bin"
    binary.write_bytes(b"\x00\xff")
    before = package(root)
    assert (
        call(
            root,
            "edit_resource",
            id="method",
            path="binary.bin",
            expected_sha256=library.digest(binary.read_bytes()),
            old_text="x",
            new_text="y",
        )["error"]
        == "binary_resource_not_editable"
    )
    assert package(root) == before
    resource = call(root, "write_resource", id="method", path="reference.txt", content="x")
    before = package(root)
    assert (
        call(
            root,
            "edit_resource",
            id="method",
            path="reference.txt",
            expected_sha256=resource["sha256"],
            old_text="x",
            new_text="z" * (library.MAX_RESOURCE_BYTES + 1),
        )["error"]
        == "resource_too_large"
    )
    assert package(root) == before


def test_partial_edit_recovers_if_catalog_publication_fails(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    saved = create(root)
    before = package(root)
    original = library.atomic_write
    failed = False

    def fail_once(path, raw):
        nonlocal failed
        if Path(path) == root / "method" / library.CATALOG and not failed:
            failed = True
            raise OSError("interrupted catalog publish")
        return original(path, raw)

    monkeypatch.setattr(library, "atomic_write", fail_once)
    result = call(
        root,
        "edit",
        id="method",
        expected_sha256=saved["sha256"],
        old_text="Check inputs.",
        new_text="Compare inputs.",
    )
    assert result["error"] == "invalid_skill_request_or_storage"
    assert failed and package(root) == before
    assert not (tmp_path / ".runtime/skill-revisions/.pending.json").exists()


def test_save_synchronizes_description_and_archived_skills_require_restore(tmp_path):
    root = tmp_path / "skills"
    saved = create(root)
    original = call(root, "read", id="method")["content"]
    updated = call(
        root,
        "save",
        id="method",
        expected_sha256=saved["sha256"],
        content=original,
        description="New applicability",
        source="trial-2",
    )
    header = yaml.safe_load(call(root, "read", id="method")["content"].split("---", 2)[1])
    assert header["description"] == "New applicability"
    archived = call(root, "archive", id="method", expected_sha256=updated["sha256"])
    assert (
        call(
            root,
            "edit",
            id="method",
            expected_sha256=archived["sha256"],
            old_text="Check",
            new_text="Compare",
        )["error"]
        == "archived_skill_requires_save"
    )
