"""Read native-session evidence as bounded UTF-8 character pages."""

import codecs
import hashlib
from pathlib import Path

from a3_research_agent.path_safety import workspace_path


def read_session_page(root: Path, name: str, offset: int = 0, limit: int = 4000) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise ValueError("Invalid session text page bounds")
    path = workspace_path(root, name)
    if not path.is_file():
        raise ValueError("Session artifact must be a regular file")
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    total, pieces, binary, size = 0, [], False, 0
    with path.open("rb") as stream:
        for raw in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(raw)
            size += len(raw)
            if binary:
                continue
            try:
                chunk = decoder.decode(raw)
            except UnicodeError:
                binary = True
                pieces = []
                continue
            begin = max(0, offset - total)
            end = max(0, min(len(chunk), offset + limit - total))
            if end > begin:
                pieces.append(chunk[begin:end])
            total += len(chunk)
    if not binary:
        try:
            tail = decoder.decode(b"", final=True)
            if total < offset + limit:
                pieces.append(tail[max(0, offset - total) : offset + limit - total])
            total += len(tail)
        except UnicodeError:
            binary = True
    metadata = {"path": name, "sha256": digest.hexdigest(), "bytes": size}
    if binary:
        return {
            **metadata,
            "encoding": "binary",
            "notice": "Binary artifact; text was not delivered.",
        }
    start = min(offset, total)
    end = min(offset + limit, total)
    return {
        **metadata,
        "encoding": "utf-8",
        "unit": "characters",
        "offset": start,
        "end": end,
        "total": total,
        "eof": end == total,
        "next_offset": None if end == total else end,
        "content": "".join(pieces),
    }
