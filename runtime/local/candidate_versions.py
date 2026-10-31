"""Keep immutable candidate commits and private worktrees on the Host."""

import hashlib
import json
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path, PurePosixPath

from runtime.local.candidate_runtime import write_candidate_files


class CandidateVersions:
    @staticmethod
    def read_snapshot(root: Path, version_id: str) -> dict[str, str]:
        """Read a saved snapshot without writing to the previous run's repository."""
        metadata = root / "versions.json"
        records = json.loads(metadata.read_text()) if metadata.is_file() else {}
        record = records.get(version_id)
        if record is None or not re.fullmatch(r"[a-f0-9]{40}", record["commit"]):
            raise ValueError("No valid retained version")

        def read(*args):
            return subprocess.check_output(
                ["git", "--git-dir", str(root / "repository.git"), *args], timeout=30
            )

        files = {}
        tree = read("ls-tree", "-rz", record["commit"], "--", "source/")
        for entry in filter(None, tree.split(b"\0")):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split()
            if mode != "100644" or kind != "blob":
                raise ValueError("Invalid retained file type")
            files[name.decode().removeprefix("source/")] = read(
                "cat-file", "blob", object_id
            ).decode("utf-8")
        digest = hashlib.sha256(
            json.dumps(files, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if digest != record["source_sha256"]:
            raise ValueError("Retained source checksum mismatch")
        return files

    def __init__(self, root: Path):
        self.root = root
        self.repository = root / "repository.git"
        self.worktrees = root / "worktrees"
        self.metadata = root / "versions.json"
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.worktrees.mkdir(exist_ok=True)
        if not self.repository.exists():
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(self.repository)],
                check=True,
                capture_output=True,
            )
        attributes = self.repository / "info/attributes"
        attributes.parent.mkdir(exist_ok=True)
        attributes.write_text("* -text -filter -ident -working-tree-encoding\n")
        self.records = json.loads(self.metadata.read_text()) if self.metadata.exists() else {}

    def git(self, *arguments: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Candidate Store",
                "-c",
                "user.email=agent@localhost",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.attributesFile=/dev/null",
                "--git-dir",
                str(self.repository),
                *arguments,
            ],
            input=input_text,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()

    def source_tree(self, directory: Path) -> str:
        entries = []
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                mode, kind, object_id = "040000", "tree", self.source_tree(path)
            else:
                mode, kind = "100644", "blob"
                object_id = self.git(
                    "hash-object",
                    "-w",
                    "--stdin",
                    "--no-filters",
                    input_text=path.read_bytes().decode("utf-8"),
                )
            entries.append(f"{mode} {kind} {object_id}\t{path.name}\0")
        return self.git("mktree", "-z", input_text="".join(entries))

    def save(self) -> None:
        temporary = self.metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.records, indent=2))
        temporary.replace(self.metadata)

    def owned(self, owner: str, version_id: str) -> dict:
        record = self.records.get(version_id)
        if record is None or owner not in record["readers"]:
            raise PermissionError("Candidate version is not accessible")
        return record

    @staticmethod
    def public(record: dict) -> dict:
        return {key: value for key, value in record.items() if key not in {"readers"}}

    def list(self, owner: str) -> list[dict]:
        with self.lock:
            return [self.public(r) for r in self.records.values() if owner in r["readers"]]

    def grant(self, owner: str, recipient: str, version_id: str) -> None:
        with self.lock:
            record = self.owned(owner, version_id)
            if recipient not in record["readers"]:
                record["readers"].append(recipient)
                self.save()

    def create(self, owner: str, files: dict[str, str], parent: str | None = None) -> dict:
        # Version only explicitly submitted source; never scan logs or credentials.
        for name in files:
            if name != PurePosixPath(name).as_posix():
                raise ValueError("Candidate paths must use canonical relative names")
            if any(
                part == ".git" or part == ".env" or part.startswith(".env.")
                for part in Path(name).parts
            ):
                raise ValueError("Git control files and environment files cannot be versioned")
        with self.lock:
            previous = self.owned(owner, parent)["commit"] if parent else None
            version_id = "version-" + uuid.uuid4().hex
            staging = self.worktrees / ("staging-" + uuid.uuid4().hex)
            staging.mkdir()
            try:
                source = staging / "source"
                source.mkdir()
                write_candidate_files(source, files)
                # Build Git blobs directly: uploaded ignore rules and attributes cannot alter bytes.
                tree = self.source_tree(staging)
                arguments = ["commit-tree", tree, "-m", version_id]
                if previous:
                    arguments += ["-p", previous]
                commit = self.git(*arguments)
                self.git("update-ref", "refs/candidates/" + version_id, commit)
            finally:
                shutil.rmtree(staging)
            record = {
                "version_id": version_id,
                "commit": commit,
                "parent_version": parent,
                "source_sha256": hashlib.sha256(
                    json.dumps(files, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                "file_count": len(files),
                "readers": [owner],
            }
            self.records[version_id] = record
            self.save()
            return self.public(record)

    def checkout(self, owner: str, version_id: str, session_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session_id):
            raise ValueError("Invalid worktree identity")
        with self.lock:
            record = self.owned(owner, version_id)
            target = self.worktrees / session_id
            self.git("worktree", "add", "--quiet", "--detach", str(target), record["commit"])
            source = target / "source"
            source.mkdir(exist_ok=True)
            return source

    def release(self, session_id: str) -> None:
        """Detach Git bookkeeping after execution; preserve all workspace evidence."""
        with self.lock:
            target = self.worktrees / session_id
            marker = target / ".git"
            if marker.is_file():
                marker.unlink()
                self.git("worktree", "prune", "--expire", "now")
