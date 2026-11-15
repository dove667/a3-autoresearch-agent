"""Freeze Git-visible reference files once per content revision."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

EXCLUDED = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".playwright-cli"}
EXCLUDED_TOP_LEVEL = {"output", "outputs", "reports", "report"}
MANIFEST = ".reference-manifest.json"


def source_files(source: Path, prefix: Path = Path()):
    """Select tracked and unignored files, including populated submodules."""
    names = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ]
        )
        .decode()
        .split("\0")
    )
    for name in sorted(set(filter(None, names))):
        relative = Path(name)
        if relative.parts[0] in EXCLUDED_TOP_LEVEL or any(p in EXCLUDED for p in relative.parts):
            continue
        path = source / relative
        if path.is_symlink():
            continue
        if path.is_dir() and (path / ".git").exists():
            yield from source_files(path, prefix / relative)
        elif path.is_file():
            yield prefix / relative, path


def verify_reference_source(directory: Path) -> None:
    manifest = json.loads((directory / MANIFEST).read_text())
    expected = manifest["files"]
    actual = {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}
    if actual != set(expected) | {MANIFEST}:
        raise ValueError("Reference cache file inventory changed")
    for name, record in expected.items():
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Reference cache content changed")
        if bool(path.stat().st_mode & 0o111) != record["executable"]:
            raise ValueError("Reference cache executable mode changed")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if directory.name != digest:
        raise ValueError("Reference cache manifest changed")


def prepare_reference_source(source: Path, cache_root: Path) -> Path:
    """Publish a shared immutable-content view without cloning the Git database."""
    source, cache_root = Path(source).resolve(), Path(cache_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reference-", dir=cache_root) as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        files = {}
        for relative, original in source_files(source):
            raw = original.read_bytes()
            executable = bool(original.stat().st_mode & 0o111)
            files[relative.as_posix()] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "executable": executable,
            }
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o755 if executable else 0o644)
        manifest = {"version": 1, "files": files}
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        destination = cache_root / digest
        (staging / MANIFEST).write_text(json.dumps(manifest, sort_keys=True))
        try:
            staging.rename(destination)
        except FileExistsError:
            pass
        except OSError:
            if not destination.is_dir():
                raise
        verify_reference_source(destination)
    return destination


def link_reference_source(source: Path, cache_root: Path, link: Path) -> Path:
    """Attach one run to its content-pinned shared reference source."""
    destination = prepare_reference_source(source, cache_root)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == destination:
        return destination
    link.symlink_to(destination, target_is_directory=True)
    return destination
