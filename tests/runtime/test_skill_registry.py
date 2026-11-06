"""Workspace skill persistence and discovery through the public tool contract."""

import json

from a3_research_agent.harness import skill_registry as skills


def call(root, action, **kwargs):
    return skills.dispatch({"action": action, **kwargs}, root=root)


def test_save_discover_read_update_and_archive(tmp_path):
    saved = call(
        tmp_path,
        "save",
        id="parallel-evaluation",
        description="Parallel independent episodes",
        content="# Parallel\nUse bounded jobs.",
        keywords=["throughput"],
        source="feedback-9",
    )
    assert saved["version"] == "0.1.0"
    assert call(tmp_path, "list", query="throughput")["skills"][0]["id"] == saved["id"]
    read = call(tmp_path, "read", id=saved["id"], limit=8)
    assert read["content"] == "# Parall" and not read["eof"]
    conflict = call(
        tmp_path, "save", id=saved["id"], description="changed", content="new", source="trial"
    )
    assert conflict["error"] == "revision_conflict"
    updated = call(
        tmp_path,
        "save",
        id=saved["id"],
        description="changed",
        content="new",
        source="trial",
        expected_sha256=saved["sha256"],
    )
    assert updated["version"] == "0.1.1"
    history = json.loads((tmp_path / "skills/index.json").read_text())["skills"][0][
        "version_history"
    ]
    assert (tmp_path / history[-1]["local_source"]).read_text() == "# Parallel\nUse bounded jobs."
    assert (
        call(tmp_path, "archive", id=saved["id"], expected_sha256=saved["sha256"])["error"]
        == "revision_conflict"
    )
    assert (
        call(tmp_path, "archive", id=saved["id"], expected_sha256=updated["sha256"])["status"]
        == "archived"
    )
    assert call(tmp_path, "list")["skills"] == []
    assert call(tmp_path, "read", id=saved["id"])["content"] == "new"


def test_legacy_catalog_preserved_and_readable(tmp_path):
    tree = tmp_path / "skills"
    tree.mkdir()
    (tree / "legacy.md").write_text("Legacy content")
    legacy = {
        "id": "old",
        "version": "0.4.1",
        "status": "reference",
        "local_source": "skills/legacy.md",
        "evidence": ["original"],
    }
    (tree / "index.json").write_text(json.dumps({"catalog_version": "1.0.2", "skills": [legacy]}))
    assert call(tmp_path, "read", id="old")["content"] == "Legacy content"
    call(tmp_path, "save", id="new", description="new", content="new", source="test")
    assert json.loads((tree / "index.json").read_text())["skills"][0] == legacy


def test_escape_and_symlink_rejected(tmp_path):
    assert call(tmp_path, "save", id="../escape", description="x", content="x", source="test")[
        "error"
    ]
    tmp_path = tmp_path / "second"
    tmp_path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "skills").symlink_to(outside)
    assert call(tmp_path, "save", id="safe", description="x", content="x", source="test")["error"]
    assert not list(outside.iterdir())


def test_unindexed_markdown_is_visible_for_registration(tmp_path):
    tree = tmp_path / "skills/local"
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text("local skill")
    result = call(tmp_path, "list")
    assert result["unindexed"] == ["skills/local/SKILL.md"]


def test_concurrent_updates_keep_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    original = call(
        tmp_path, "save", id="shared", description="shared", content="original", source="test"
    )

    def update(content):
        return call(
            tmp_path,
            "save",
            id="shared",
            description="shared",
            content=content,
            source="test",
            expected_sha256=original["sha256"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first", "second"]))
    assert sum(r.get("error") == "revision_conflict" for r in results) == 1
    assert call(tmp_path, "read", id="shared")["content"] in {"first", "second"}


def test_bad_catalog_is_preserved(tmp_path):
    tree = tmp_path / "skills"
    tree.mkdir()
    p = tree / "index.json"
    p.write_text('{"skills": "broken"}')
    assert call(tmp_path, "save", id="new", description="x", content="x", source="test")["error"]
    assert p.read_text() == '{"skills": "broken"}'


def test_harness_dispatch_and_payload_include_skill_manager(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent
    from a3_research_agent.packaging.harness_payload import build_harness_payload

    monkeypatch.chdir(tmp_path)
    assert "a3_research_agent/harness/skill_registry.py" in build_harness_payload()
    assert sum(t["name"] == "skills" for t in agent.tools) == 1
    saved = agent.tool(
        "skills",
        {
            "action": "save",
            "id": "dispatch",
            "description": "test",
            "content": "text",
            "source": "test",
        },
    )
    assert saved["version"] == "0.1.0"
    assert agent.tool("skills", {"action": "read", "id": "dispatch"})["content"] == "text"


def test_updates_preserve_relative_references(tmp_path):
    old = call(
        tmp_path,
        "save",
        id="linked",
        description="linked",
        content="[ref](references/usage.md)",
        source="test",
    )
    reference = tmp_path / "skills/linked/references/usage.md"
    reference.parent.mkdir()
    reference.write_text("usage")
    new = call(
        tmp_path,
        "save",
        id="linked",
        description="linked",
        content="[ref](references/usage.md) updated",
        source="test",
        expected_sha256=old["sha256"],
    )
    assert new["local_source"] == old["local_source"]
    assert (tmp_path / new["local_source"]).parent.joinpath(
        "references/usage.md"
    ).read_text() == "usage"
    assert call(tmp_path, "list")["unindexed"] == []


def test_legacy_metadata_can_archive_but_fixed_backend_is_protected(tmp_path):
    tree = tmp_path / "skills"
    tree.mkdir()
    entries = [
        {"id": "pending", "status": "pending", "local_source": None},
        {
            "id": "synthetic-report-checker",
            "status": "local-function",
            "local_source": "skill_tools.py:check_report",
        },
    ]
    (tree / "index.json").write_text(json.dumps({"skills": entries}))
    for name in ["pending", "synthetic-report-checker"]:
        current = call(tmp_path, "read", id=name)
        result = call(tmp_path, "archive", id=name, expected_sha256=current["sha256"])
        assert (
            result.get("status") == "archived"
            if name == "pending"
            else result["error"] == "fixed_executable_entry_read_only"
        )


def test_catalog_write_failure_restores_active_body(tmp_path, monkeypatch):
    old = call(tmp_path, "save", id="recover", description="x", content="original", source="test")
    before = (tmp_path / "skills/index.json").read_bytes()
    write = skills.atomic_write

    def fail_index(path, data):
        if path.name == "index.json":
            raise OSError("disk failure")
        write(path, data)

    monkeypatch.setattr(skills, "atomic_write", fail_index)
    result = call(
        tmp_path,
        "save",
        id="recover",
        description="x",
        content="changed",
        source="test",
        expected_sha256=old["sha256"],
    )
    assert result["error"]
    assert (tmp_path / old["local_source"]).read_text() == "original"
    assert (tmp_path / "skills/index.json").read_bytes() == before


def test_existing_host_reference_with_versioned_id_is_readable(tmp_path):
    from a3_research_agent.harness.skill_registry import dispatch

    directory = tmp_path / "skills"
    directory.mkdir()
    (directory / "SKILL.md").write_text("Existing deployed reference")
    import json

    (directory / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {
                        "id": "reference-0.1.6-hash",
                        "version": "0.1.6",
                        "status": "reference",
                        "local_source": "skills/SKILL.md",
                        "description": "Existing reference",
                    }
                ],
            }
        )
    )
    read = dispatch({"action": "read", "id": "reference-0.1.6-hash"}, root=tmp_path)
    assert read["content"] == "Existing deployed reference"
    create = dispatch(
        {
            "action": "save",
            "id": "new-0.1.0",
            "description": "New",
            "source": "test",
            "content": "New skill",
        },
        root=tmp_path,
    )
    assert create["error"] == "invalid_skill_id"
