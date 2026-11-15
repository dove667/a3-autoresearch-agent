"""Research source attachments and current session layouts have explicit bounds."""

import hashlib

import pytest

from a3_research_agent.host.sessions.workspace import (
    read_workspace_files,
    session_workspace,
    write_workspace_files,
)


def test_current_session_link_resolves_only_to_its_research_directory(tmp_path):
    root = tmp_path / "artifacts/rsi/run-unit/sessions"
    directory = root / "main"
    directory.mkdir(parents=True)
    target = tmp_path / "workspace/run-unit/main"
    target.mkdir(parents=True)
    (directory / "workspace").symlink_to(target, target_is_directory=True)
    assert session_workspace(root, "main") == target
    (directory / "workspace").unlink()
    (directory / "workspace").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="current Host-managed"):
        session_workspace(root, "main")


def test_workspace_writes_reject_escape_and_symbolic_links(tmp_path):
    (tmp_path / "alias").symlink_to(tmp_path.parent, target_is_directory=True)
    for files in ({"../outside": "bad"}, {"/outside": "bad"}, {"alias/outside": "bad"}):
        with pytest.raises(ValueError):
            write_workspace_files(tmp_path, files)
    write_workspace_files(tmp_path, {"research/run.py": "print('research')"})
    assert (tmp_path / "research/run.py").read_text() == "print('research')"


@pytest.mark.parametrize(
    "kind", ["hash", "root", "traversal", "symlink", "binary", "oversized", "count"]
)
def test_source_attachments_are_validated_before_delivery(tmp_path, kind):
    source = tmp_path / "research/run.py"
    source.parent.mkdir()
    source.write_bytes(b"print('research')")
    references = [
        {"path": "research/run.py", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    ]
    if kind == "hash":
        references[0]["sha256"] = "0" * 64
    elif kind == "root":
        references[0]["path"] = "checkpoint.json"
    elif kind == "traversal":
        references[0]["path"] = "research/../../outside"
    elif kind == "symlink":
        source.unlink()
        source.symlink_to(tmp_path / "outside.py")
    elif kind == "binary":
        source.write_bytes(b"\xff")
        references[0]["sha256"] = hashlib.sha256(b"\xff").hexdigest()
    elif kind == "oversized":
        source.write_bytes(b"x" * (1024 * 1024 + 1))
    else:
        references *= 17
    with pytest.raises(ValueError):
        read_workspace_files(tmp_path, references)


def test_source_delivery_hash_identifies_actual_bytes(tmp_path):
    source = tmp_path / "research/run.py"
    source.parent.mkdir()
    source.write_bytes(b"VALUE = 7\r\n")
    digest = hashlib.sha256(b"VALUE = 7\r\n").hexdigest()
    files, receipt = read_workspace_files(tmp_path, [{"path": "research/run.py", "sha256": digest}])
    assert files == {"research/run.py": "VALUE = 7\r\n"}
    assert receipt == [{"path": "research/run.py", "sha256": digest, "bytes": 11}]
