"""Memory provenance reads declared sources without expanding filesystem access."""

import gzip
import hashlib
import io
import json
import tarfile

from a3_research_agent.harness.memory.sources import MemorySources


def imported(root, entry):
    raw = json.dumps(entry, ensure_ascii=False).encode() + b"\n"
    archive = "trajectories/2026-11-10/conversation/memory-original.jsonl.gz"
    path = root / archive
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(raw))
    (root / "memory").mkdir()
    (root / "memory/sources.json").write_text(
        json.dumps(
            [{"archive": archive, "sha256": hashlib.sha256(raw).hexdigest(), "ids": [entry["id"]]}]
        )
    )
    return path


def history(root, files):
    archive = root / "trajectories/2026-11-10/model-records.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "files": {}}
    with tarfile.open(archive, "w:gz") as stream:
        for name, raw in files.items():
            digest = hashlib.sha256(raw).hexdigest()
            manifest["files"][name] = {"bytes": len(raw), "sha256": digest}
            member = tarfile.TarInfo("blobs/" + digest)
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
        raw = json.dumps(manifest).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(raw)
        stream.addfile(member, io.BytesIO(raw))
    return archive


def test_original_note_is_verified_and_labelled_separately_from_research_evidence(tmp_path):
    entry = {"id": "a" * 12, "text": "旧模型记录", "refs": []}
    archive = imported(tmp_path, entry)
    reader = MemorySources(tmp_path)
    assert reader(entry, {})["sources"][0]["kind"] == "original_note"
    result = reader(entry, {"ref": "imported-original"})
    assert json.loads(result["content"])["text"] == "旧模型记录"
    assert result["kind"] == "original_note"
    archive.write_bytes(gzip.compress(b"changed"))
    assert reader(entry, {"ref": "imported-original"})["error"] == "memory_source_unavailable"


def test_declared_sources_are_paged_and_other_project_files_are_inaccessible(tmp_path):
    path = tmp_path / "workspace/experiments/trial/report.md"
    path.parent.mkdir(parents=True)
    path.write_text("结果abcdef")
    entry = {"id": "a" * 12, "refs": ["workspace/experiments/trial/report.md"]}
    reader = MemorySources(tmp_path)
    result = reader(entry, {"ref": entry["refs"][0], "offset": 2, "limit": 3})
    assert result["content"] == "abc" and (not result["eof"])
    assert reader(entry, {"ref": ".env"})["error"] == "memory_source_unavailable"
    entry["refs"] = ["workspace/../.env"]
    assert reader(entry, {"ref": entry["refs"][0]})["error"] == "memory_source_unavailable"


def test_workspace_symlink_does_not_expand_read_access(tmp_path):
    path = tmp_path / "workspace/scripts/leak.py"
    path.parent.mkdir(parents=True)
    private = tmp_path / ".env"
    private.write_text("secret")
    path.symlink_to(private)
    entry = {"id": "a" * 12, "refs": ["workspace/scripts/leak.py"]}
    assert (
        MemorySources(tmp_path)(entry, {"ref": entry["refs"][0]})["error"]
        == "memory_source_unavailable"
    )


def test_streamed_history_uses_manifest_hash_without_extracting_members(tmp_path):
    reference = "trajectories/2026-11-10/conversation/context-history/result.json"
    history(tmp_path, {"conversation/context-history/result.json": b'{"score":3}'})
    entry = {"id": "a" * 12, "refs": [reference]}
    result = MemorySources(tmp_path)(entry, {"ref": reference})
    assert result["content"] == '{"score":3}'
    assert result["original_path"] == "conversation/context-history/result.json"
    assert not (tmp_path / "trajectories/2026-11-10/blobs").exists()


def test_imported_relative_history_is_resolved_with_original_path(tmp_path):
    entry = {"id": "a" * 12, "refs": ["context-history/result.json"]}
    imported(tmp_path, entry)
    history(tmp_path, {"conversation/context-history/result.json": b"actual result"})
    result = MemorySources(tmp_path)(entry, {"ref": entry["refs"][0]})
    assert result["content"] == "actual result"
    assert result["original_path"] == "conversation/context-history/result.json"


def test_job_and_memory_locators_preserve_tool_boundaries(tmp_path):
    job = "b" * 32
    entry = {"id": "a" * 12, "refs": ["run-20261110/jobs/" + job, "hypothesis:" + "c" * 12]}
    reader = MemorySources(tmp_path)
    assert reader(entry, {"ref": entry["refs"][0]})["path"] == "/jobs/" + job
    assert reader(entry, {"ref": entry["refs"][1]})["id"] == "c" * 12
