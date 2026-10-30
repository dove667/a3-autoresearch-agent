"""Export a previous run's research artifacts for a fresh execution identity."""

import hashlib
import json
from pathlib import Path

AUTH_KEYS = {
    "a3_gateway_token",
    "openai_api_key",
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
}
PRIVATE_NAMES = {".git", ".venv", ".ssh", ".config", "__pycache__", "auth.json"}


def stage_recovery(parent: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    trajectory = parent / "trajectory.json"
    warnings = []
    try:
        previous = json.loads(trajectory.read_text()) if trajectory.exists() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        previous = {}
        warnings.append(
            "Root trajectory is incomplete; recover other artifacts and retain the original for review."
        )
    secrets = set()

    def collect_auth(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in AUTH_KEYS and isinstance(item, str) and len(item) >= 8:
                    secrets.add(item)
                else:
                    collect_auth(item)
        elif isinstance(value, list):
            for item in value:
                collect_auth(item)

    collect_auth(previous.get("info", {}))

    sources = [
        (parent / "workspace/run", Path("bootstrap")),
        (parent / "recovery", Path("earlier")),
    ]
    for candidate in sorted((parent / "candidates").glob("*")):
        if not candidate.is_symlink():
            sources.append((candidate / "workspace", Path("candidates") / candidate.name))
    evidence = []
    parsed = {}
    for source, prefix in sources:
        if not source.is_dir() or source.is_symlink():
            continue
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in PRIVATE_NAMES or part.startswith(".env") for part in relative.parts):
                continue
            if not path.is_file() or path.is_symlink():
                continue
            evidence.append((path, prefix / relative))
            if path.suffix not in {".json", ".jsonl"}:
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if path.suffix == ".jsonl":
                rows = []
                for number, line in enumerate(text.splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        warnings.append(
                            f"Skipped incomplete JSONL fragment: {prefix / relative}:{number}"
                        )
                parsed[path] = rows
            else:
                try:
                    parsed[path] = json.loads(text)
                except json.JSONDecodeError:
                    warnings.append(f"Incomplete JSON retained as text: {prefix / relative}")
            if path in parsed:
                collect_auth(parsed[path])

    def clean(value):
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower() in AUTH_KEYS else clean(item)
                for key, item in value.items()
                if key != "encrypted_content"
            }
        if isinstance(value, list):
            return [
                clean(item)
                for item in value
                if not isinstance(item, dict) or item.get("type") != "reasoning"
            ]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    files = []

    def save(relative, content):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files.append(
            {
                "path": str(relative),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    for path, relative in evidence:
        raw = path.read_bytes()
        if path in parsed:
            value = clean(parsed[path])
            text = (
                "\n".join(json.dumps(row, ensure_ascii=False) for row in value) + "\n"
                if path.suffix == ".jsonl"
                else json.dumps(value, ensure_ascii=False)
            )
            raw = text.encode()
        else:
            try:
                raw = clean(raw.decode("utf-8")).encode()
            except UnicodeDecodeError:
                pass
        save(relative, raw)
    if previous:
        visible = {
            "exit_status": previous.get("info", {}).get("exit_status"),
            "messages": clean(previous.get("messages", [])),
        }
        save(Path("bootstrap-trajectory.json"), json.dumps(visible, ensure_ascii=False).encode())
    events = parent / "session-events.jsonl"
    if events.is_file():
        save(Path("session-events.jsonl"), events.read_bytes())
    index = {
        "parent_run": parent.name,
        "previous_exit": previous.get("info", {}).get("exit_status"),
        "files": files,
        "warnings": warnings,
        "execution": "New sessions and credentials are required; previous job and session IDs are historical references.",
        "access": "Read-only bootstrap recovery archive. Pass selected ancestor evidence explicitly to successors; do not expose unrelated private candidate directories.",
    }
    (destination / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    return index
