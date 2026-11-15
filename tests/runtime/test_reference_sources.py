import subprocess

import pytest

from a3_research_agent.host.reference_sources import (
    link_reference_source,
    prepare_reference_source,
    verify_reference_source,
)


def make_repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text(".env\nprivate/\n")
    (path / "module.py").write_text("VERSION = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Baseline",
        ],
        check=True,
    )


def test_shared_cache_preserves_dirty_files_and_reuses_content(tmp_path):
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    make_repository(source)
    (source / ".env").write_text("PRIVATE_CREDENTIAL")
    (source / "module.py").write_text("VERSION = 2\n")
    (source / "new.py").write_text("NEW = True\n")
    status = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"])
    first = prepare_reference_source(source, cache)
    assert (first / "module.py").read_text() == "VERSION = 2\n"
    assert (first / "new.py").is_file()
    assert not (first / ".env").exists()
    assert not (first / ".git").exists()
    assert prepare_reference_source(source, cache) == first
    assert subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"]) == status
    (source / "module.py").write_text("VERSION = 3\n")
    second = prepare_reference_source(source, cache)
    assert second != first
    assert (first / "module.py").read_text() == "VERSION = 2\n"
    (source / "new.py").unlink()
    assert not (prepare_reference_source(source, cache) / "new.py").exists()


def test_reference_excludes_generated_material_even_when_tracked(tmp_path):
    source = tmp_path / "source"
    make_repository(source)
    for directory in ("reports", "output", ".playwright-cli"):
        (source / directory).mkdir()
        (source / directory / "generated.txt").write_text("temporary output")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    cached = prepare_reference_source(source, tmp_path / "cache")
    assert not (cached / "reports").exists()
    assert not (cached / "output").exists()
    assert not (cached / ".playwright-cli").exists()
    (source / "module.py").unlink()
    assert not (prepare_reference_source(source, tmp_path / "cache") / "module.py").exists()


def test_each_run_reference_link_resolves_to_one_frozen_cache(tmp_path):
    source = tmp_path / "source"
    make_repository(source)
    cache = tmp_path / ".runtime/reference-sources"
    first = tmp_path / "trajectories/first/public/a3"
    second = tmp_path / "trajectories/second/public/a3"
    pinned = link_reference_source(source, cache, first)
    assert link_reference_source(source, cache, second) == pinned
    assert first.is_symlink() and second.is_symlink()
    assert first.resolve() == second.resolve()
    (source / "module.py").write_text("new source")
    assert (first / "module.py").read_text() == "VERSION = 1\n"
    assert link_reference_source(source, cache, tmp_path / "trajectories/third/public/a3") != pinned
    verify_reference_source(first.resolve())


def test_modified_cached_content_is_rejected(tmp_path):
    source = tmp_path / "source"
    make_repository(source)
    cache = tmp_path / "cache"
    pinned = prepare_reference_source(source, cache)
    (pinned / "module.py").write_text("corrupted")
    with pytest.raises(ValueError, match="content changed"):
        prepare_reference_source(source, cache)
