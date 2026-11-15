"""The single project library supports progressive reads and versioned package edits."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from a3_research_agent.harness import skill_library
from a3_research_agent.harness import skill_registry as skills


def call(root, action, **arguments):
    return skills.dispatch({"action": action, **arguments}, root=root)


def create(root, identifier="method", **changes):
    return call(
        root,
        "save",
        id=identifier,
        description="Training method",
        source="trial-1",
        content="# Method\nFollow the evidence.",
        keywords=["training"],
        **changes,
    )


def test_save_discover_read_update_archive_and_restore(tmp_path):
    root = tmp_path / "skills"
    saved = create(root)
    assert saved["version"] == "0.1.0"
    assert not (root / "index.json").exists()
    assert call(root, "list", query="training")["skills"][0]["id"] == "method"
    original = call(root, "read", id="method")
    assert original["content"].endswith("Follow the evidence.")
    assert original["sha256"] == saved["sha256"]
    short = call(root, "read", id="method", limit=8)
    assert short["content"] == "---\nname" and not short["eof"]
    assert create(root)["error"] == "revision_conflict"
    updated = call(
        root,
        "save",
        id="method",
        description="New method",
        source="trial-2",
        content="# Improved\nUse matched conditions.",
        expected_sha256=saved["sha256"],
    )
    assert updated["version"] == "0.1.1"
    archive = call(root, "archive", id="method", expected_sha256=updated["sha256"])
    assert archive["status"] == "archived" and archive["version"] == "0.1.2"
    assert call(root, "list")["skills"] == []
    assert call(root, "list", include_archived=True)["total"] == 1
    assert call(root, "read", id="method")["content"].endswith("Use matched conditions.")
    restored = call(
        root,
        "save",
        id="method",
        description="Restored",
        source="trial-3",
        content="Restored content",
        expected_sha256=archive["sha256"],
    )
    assert restored["status"] == "active" and restored["version"] == "0.1.3"
    entry = json.loads((root / "method/assets/catalog-entry.json").read_text())
    frontmatter = yaml.safe_load((root / "method/SKILL.md").read_text().split("---", 2)[1])
    assert entry["version"] == frontmatter["metadata"]["version"] == restored["version"]


def test_progressive_discovery_does_not_deliver_or_read_bodies(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    create(root)
    (root / "README.md").write_text("Root navigation")
    monkeypatch.setattr(skill_library, "read_body", lambda *args: pytest.fail("body loaded"))
    listing = call(root, "list")
    assert listing["total"] == 1 and "Follow the evidence" not in str(listing)
    assert set(listing["skills"][0]) == {"id", "version", "status", "description"}


def test_resource_crud_preserves_whole_package_revisions(tmp_path):
    root = tmp_path / "skills"
    original = create(root)
    write = call(root, "write_resource", id="method", path="references/usage.md", content="αβγ\n")
    assert write["version"] == "0.1.1"
    assert call(root, "list_resources", id="method")["resources"] == [
        {"path": "references/usage.md", "size_bytes": len("αβγ\n".encode())}
    ]
    page = call(root, "read_resource", id="method", path="references/usage.md", offset=1, limit=2)
    assert page["content"] == "βγ" and page["unit"] == "characters"
    assert "Follow the evidence" not in str(page)
    assert (
        call(root, "archive", id="method", expected_sha256=original["sha256"])["error"]
        == "revision_conflict"
    )
    assert (
        call(root, "write_resource", id="method", path="references/usage.md", content="new")[
            "error"
        ]
        == "revision_conflict"
    )
    changed = call(
        root,
        "write_resource",
        id="method",
        path="references/usage.md",
        content="new",
        expected_sha256=page["sha256"],
    )
    assert changed["version"] == "0.1.2"
    history = tmp_path / ".runtime/skill-revisions" / changed["history_revision"]
    snapshot = json.loads((history / ".snapshot.json").read_text())
    assert (history / "references/usage.md").read_text() == "αβγ\n"
    for name, digest in snapshot["files"].items():
        assert hashlib.sha256((history / name).read_bytes()).hexdigest() == digest
    deleted = call(
        root,
        "delete_resource",
        id="method",
        path="references/usage.md",
        expected_sha256=changed["sha256"],
    )
    assert deleted["version"] == "0.1.3"
    assert call(root, "list_resources", id="method")["resources"] == []
    assert (
        call(root, "read_resource", id="method", path="references/usage.md")["error"]
        == "resource_not_found"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "./reference.md",
        "/tmp/escape",
        "a\\b",
        ".secret",
        "revisions/old.md",
        "SKILL.md",
        "skill.md",
        "ASSETS/CATALOG-ENTRY.JSON",
        "assets/catalog-entry.json",
    ],
)
def test_resource_paths_cannot_replace_metadata_or_escape(tmp_path, path):
    root = tmp_path / "skills"
    create(root)
    before = (root / "method/SKILL.md").read_bytes()
    assert call(root, "write_resource", id="method", path=path, content="bad")["error"]
    assert (root / "method/SKILL.md").read_bytes() == before


def test_symlink_roots_packages_and_attachments_are_rejected(tmp_path):
    root, outside = tmp_path / "skills", tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside)
    assert create(root)["error"] == "symlink_rejected"
    root.unlink()
    create(root)
    (root / "method/references").symlink_to(outside, target_is_directory=True)
    assert (
        call(root, "write_resource", id="method", path="references/file.md", content="bad")["error"]
        == "symlink_rejected"
    )
    assert not list(outside.iterdir())
    (root / "linked").symlink_to(root / "method", target_is_directory=True)
    assert call(root, "read", id="linked")["error"] == "symlink_rejected"


def test_host_roots_are_required_and_working_directory_does_not_select_library(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("A3_SKILLS_ROOT", raising=False)
    assert skills.dispatch({"action": "list"})["error"] == "host_skills_root_required"
    root = tmp_path / "library"
    monkeypatch.setenv("A3_SKILLS_ROOT", str(root))
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(tmp_path / "skill-history"))
    first = skills.dispatch(
        {
            "action": "save",
            "id": "shared",
            "description": "Shared",
            "source": "trial",
            "content": "Source text",
        }
    )
    another = tmp_path / "another"
    another.mkdir()
    monkeypatch.chdir(another)
    assert skills.dispatch({"action": "read", "id": "shared"})["sha256"] == first["sha256"]
    assert not (another / "skills").exists()


def test_concurrent_updates_keep_one_winner(tmp_path):
    root = tmp_path / "skills"
    original = create(root)

    def update(content):
        return call(
            root,
            "save",
            id="method",
            description="Updated",
            source="trial",
            content=content,
            expected_sha256=original["sha256"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first", "second"]))
    assert sum(result.get("error") == "revision_conflict" for result in results) == 1


def test_failed_catalog_publication_restores_body_and_resource(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    original = create(root)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    write = skill_library.atomic_write
    target = root / "method/assets/catalog-entry.json"
    failed = False

    def fail_catalog(path, data):
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise OSError("controlled disk failure")
        write(path, data)

    monkeypatch.setattr(skill_library, "atomic_write", fail_catalog)
    result = call(root, "write_resource", id="method", path="references/new.md", content="new")
    assert result["error"]
    assert (root / "method/SKILL.md").read_bytes() == before[Path("method/SKILL.md")]
    assert not (root / "method/references/new.md").exists()
    assert call(root, "read", id="method")["sha256"] == original["sha256"]


def test_invalid_catalog_is_preserved(tmp_path):
    root = tmp_path / "skills"
    create(root)
    target = root / "method/assets/catalog-entry.json"
    target.write_text("invalid metadata")
    assert create(root)["error"]
    assert target.read_text() == "invalid metadata"


def test_search_ranks_multiword_relevance(tmp_path):
    root = tmp_path / "skills"
    create(root, "generic-training")
    call(
        root,
        "save",
        id="act-data",
        description="ACT training and DAgger data",
        content="method",
        keywords=["quality"],
        source="trial",
    )
    assert [
        entry["id"] for entry in call(root, "list", query="ACT training DAgger audit")["skills"]
    ] == ["act-data", "generic-training"]


@pytest.mark.parametrize(
    "action",
    ["create-resource", "replace-resource", "delete-resource", "save", "archive", "restore"],
)
def test_failed_mutation_restores_all_active_package_files(tmp_path, monkeypatch, action):
    root = tmp_path / "skills"
    create(root)
    resource = call(
        root,
        "write_resource",
        id="method",
        path="references/existing.md",
        content="original attachment",
    )
    body = call(root, "read", id="method")
    if action == "restore":
        call(root, "archive", id="method", expected_sha256=body["sha256"])
        body = call(root, "read", id="method")
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    original_write = skill_library.atomic_write
    failed = False

    def fail_catalog(path, data):
        nonlocal failed
        if path == root / "method/assets/catalog-entry.json" and not failed:
            failed = True
            raise OSError("controlled publication failure")
        original_write(path, data)

    monkeypatch.setattr(skill_library, "atomic_write", fail_catalog)
    if action == "create-resource":
        result = call(root, "write_resource", id="method", path="references/new.md", content="new")
    elif action == "replace-resource":
        result = call(
            root,
            "write_resource",
            id="method",
            path="references/existing.md",
            content="replacement",
            expected_sha256=resource["sha256"],
        )
    elif action == "delete-resource":
        result = call(
            root,
            "delete_resource",
            id="method",
            path="references/existing.md",
            expected_sha256=resource["sha256"],
        )
    elif action == "archive":
        result = call(root, "archive", id="method", expected_sha256=body["sha256"])
    else:
        result = call(
            root,
            "save",
            id="method",
            description="Updated",
            content="changed",
            source="trial",
            expected_sha256=body["sha256"],
        )
    assert result["error"]
    assert {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before
    assert call(root, "read", id="method")["sha256"] == body["sha256"]


def test_new_package_failure_does_not_leave_an_undiscoverable_directory(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    original_write = skill_library.atomic_write

    def fail_catalog(path, data):
        if path == root / "method/assets/catalog-entry.json":
            raise OSError("controlled publication failure")
        original_write(path, data)

    monkeypatch.setattr(skill_library, "atomic_write", fail_catalog)
    assert create(root)["error"]
    assert not (root / "method").exists()
    assert call(root, "list")["skills"] == []


def test_attachment_case_alias_cannot_create_a_second_name_or_overwrite(tmp_path):
    root = tmp_path / "skills"
    create(root)
    resource = call(
        root, "write_resource", id="method", path="references/usage.md", content="original"
    )
    for path in ("references/Usage.md", "REFERENCES/usage.md"):
        result = call(
            root,
            "write_resource",
            id="method",
            path=path,
            content="replacement",
            expected_sha256=resource["sha256"],
        )
        assert result["error"] == "skill_path_alias"
    assert (root / "method/references/usage.md").read_text() == "original"
    assert call(root, "list_resources", id="method")["total"] == 1


def test_normalization_alias_uses_the_existing_disk_name(tmp_path):
    root = tmp_path / "skills"
    create(root)
    resource = call(
        root, "write_resource", id="method", path="references/café.md", content="original"
    )
    disk_path = root / "method/references/café.md"
    alias = root / "method/references/cafe\u0301.md"
    if not alias.exists() or not alias.samefile(disk_path):
        pytest.skip("Filesystem keeps Unicode normalization spellings distinct")
    assert (
        call(root, "read_resource", id="method", path="references/cafe\u0301.md")["error"]
        == "skill_path_alias"
    )
    changed = call(
        root,
        "write_resource",
        id="method",
        path="references/cafe\u0301.md",
        content="changed",
        expected_sha256=resource["sha256"],
    )
    assert changed["error"] == "skill_path_alias"
    assert disk_path.read_text() == "original"
    assert call(root, "list_resources", id="method")["resources"][0]["path"] == "references/café.md"
