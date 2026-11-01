"""Persist explicitly authorized human feedback at stable model-history boundaries."""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

PROTOCOL_VERSION = "0.1.0"
MAX_FEEDBACK_BYTES = 64 * 1024


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def require_completed_tools(messages: list[dict]) -> None:
    pending = set()
    for message in messages:
        if message.get("type") == "function_call":
            pending.add(message.get("call_id"))
        elif message.get("type") == "function_call_output":
            pending.discard(message.get("call_id"))
    if pending:
        raise ValueError(
            "Human feedback requires completed tool results before the next model call"
        )


class HumanFeedback:
    def __init__(self, run: Path):
        self.directory = run / ".host" / "human-feedback"
        self.state_file = self.directory / "state.json"
        self.lock = threading.RLock()
        self.records = (
            json.loads(self.state_file.read_text())["records"] if self.state_file.exists() else []
        )

    def save(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        try:
            with open(temporary, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                json.dump({"protocol_version": PROTOCOL_VERSION, "records": self.records}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.state_file)
        finally:
            temporary.unlink(missing_ok=True)

    def event(self, event: str, identifier: str, **fields) -> None:
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": event, "feedback_id": identifier, **fields}) + "\n")

    def register(self, path: Path, owner: str) -> dict:
        with path.open("rb") as stream:
            content = stream.read(MAX_FEEDBACK_BYTES + 1)
        if not content.strip() or len(content) > MAX_FEEDBACK_BYTES:
            raise ValueError("Human feedback must contain 1 to 65536 bytes of UTF-8 text")
        text = content.decode("utf-8")
        sha256 = hashlib.sha256(content).hexdigest()
        with self.lock:
            previous = next((x for x in reversed(self.records) if x["owner"] == owner), None)
            if previous and previous["sha256"] == sha256:
                return {"feedback_id": previous["feedback_id"], "sha256": sha256, "new": False}
            identifier = f"feedback-{len(self.records) + 1:04d}"
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            snapshot = self.directory / f"{identifier}.md"
            with open(snapshot, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            record = {
                "feedback_id": identifier,
                "owner": owner,
                "sha256": sha256,
                "text": text,
                "registered_at": datetime.now(UTC).isoformat(),
            }
            self.records.append(record)
            self.save()
            self.event("registered", identifier, owner=owner, sha256=sha256)
            return {"feedback_id": identifier, "sha256": sha256, "new": True}

    @staticmethod
    def message(record: dict) -> dict:
        return {
            "role": "user",
            "content": (
                f"Human feedback ({PROTOCOL_VERSION}, {record['feedback_id']})\n\n" + record["text"]
            ),
        }

    def apply(self, owner: str, arguments: dict) -> tuple[dict, list[str]]:
        with self.lock:
            records = [x for x in self.records if x["owner"] == owner]
            if not records:
                return arguments, []
            original = arguments.get("input")
            if arguments.get("previous_response_id") or not isinstance(original, list):
                raise ValueError("Human feedback requires a full message-history input")
            if any(not isinstance(x, dict) for x in original):
                raise ValueError("Human feedback requires message objects")
            require_completed_tools(original)
            messages = original.copy()
            inserted = []
            identifiers = []
            for record in records:
                message = self.message(record)
                identifiers.append(record["feedback_id"])
                if message in original:
                    continue
                anchor = record.get("anchor")
                valid = bool(
                    anchor
                    and anchor["length"] <= len(original)
                    and digest(original[: anchor["length"]]) == anchor["prefix_sha256"]
                )
                if not valid:
                    record["anchor"] = {
                        "length": len(original),
                        "prefix_sha256": digest(original),
                    }
                    self.save()
                    self.event(
                        "anchored" if anchor is None else "reanchored",
                        record["feedback_id"],
                        history_length=len(original),
                    )
                position = record["anchor"]["length"]
                offset = sum(previous <= position for previous in inserted)
                messages.insert(position + offset, message)
                inserted.append(position)
            return {**arguments, "input": messages}, identifiers

    def forwarded(self, identifiers: list[str]) -> None:
        with self.lock:
            for record in self.records:
                if record["feedback_id"] in identifiers and not record.get("forwarded_at"):
                    record["forwarded_at"] = datetime.now(UTC).isoformat()
                    self.save()
                    self.event("forwarded", record["feedback_id"])
