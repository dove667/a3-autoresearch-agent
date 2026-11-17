"""Read the shared long-term memory and skill library for human review."""

import json
from pathlib import Path

from .readers import read_json, read_text

MEMORY_DEFAULTS = {
    "revision": 1,
    "kind": "note",
    "scope": "",
    "refs": [],
    "supersedes": "",
    "status": "active",
    "lifetime": "durable",
    "run_ids": [],
    "created_run": "",
    "tags": [],
}

KIND_LABELS = {
    "lesson": "经验",
    "note": "记录",
    "result": "结果",
    "finding": "发现",
    "experiment": "实验",
    "experiment-progress": "实验进展",
    "experiment-result": "实验结果",
    "experiment-prereg": "实验预设",
    "experiment-correction": "实验修正",
    "hypothesis": "假设",
    "evidence": "证据",
    "diagnostic-evidence": "诊断证据",
    "feedback": "人工反馈",
    "incident": "异常",
    "failure": "失败",
    "observation": "观察",
    "decision": "决策",
}


def load_memory(root):
    """Return the latest revision of every note plus its visible status."""
    root = Path(root)
    entries = {}
    try:
        lines = (root / "entries.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        identifier, text = event.get("id"), event.get("text")
        if not isinstance(identifier, str) or not isinstance(text, str):
            continue
        entries[identifier] = {**MEMORY_DEFAULTS, **event}

    superseded = {}
    for entry in entries.values():
        previous = entry.get("supersedes")
        if isinstance(previous, str) and previous:
            superseded.setdefault(previous, []).append(entry["id"])

    items = []
    for entry in entries.values():
        status = entry.get("status", "active")
        replaced_by = superseded.get(entry["id"], [])
        if status == "active" and not replaced_by:
            visible = "effective"
        elif replaced_by:
            visible = "superseded"
        else:
            visible = "archived"
        items.append(
            {
                "id": entry["id"],
                "kind": entry.get("kind") or "note",
                "kind_label": KIND_LABELS.get(entry.get("kind"), entry.get("kind") or "记录"),
                "scope": entry.get("scope") or "",
                "text": entry.get("text") or "",
                "tags": [tag for tag in entry.get("tags") or [] if isinstance(tag, str)],
                "refs": [ref for ref in entry.get("refs") or [] if isinstance(ref, str)],
                "ts": entry.get("ts"),
                "status": visible,
                "status_label": {
                    "effective": "有效",
                    "superseded": "已被替代",
                    "archived": "已归档",
                }[visible],
                "lifetime": entry.get("lifetime", "durable"),
                "lifetime_label": "跨运行"
                if entry.get("lifetime") == "durable"
                else "仅当前运行",
                "replaced_by": replaced_by,
            }
        )
    items.sort(key=lambda item: item.get("ts") or 0, reverse=True)
    counts = {
        "effective": sum(item["status"] == "effective" for item in items),
        "superseded": sum(item["status"] == "superseded" for item in items),
        "archived": sum(item["status"] == "archived" for item in items),
        "total": len(items),
    }
    return {"items": items, "counts": counts}


def _skill_resources(directory):
    result = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in {"SKILL.md", "assets/catalog-entry.json"}:
            continue
        if any(part.startswith(".") or part in {"__pycache__", "revisions"} for part in path.parts):
            continue
        result.append(relative)
    return result[:20]


def _without_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip("\n")
    return text


def load_skills(root):
    """Read package metadata and bodies; the Agent remains the only editor."""
    root = Path(root)
    items = []
    try:
        directories = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        directories = []
    for directory in sorted(directories, key=lambda path: path.name):
        if directory.name.startswith(".") or directory.name == "__pycache__":
            continue
        catalog = read_json(directory / "assets/catalog-entry.json", {})
        if not isinstance(catalog, dict) or catalog.get("id") != directory.name:
            continue
        body = _without_frontmatter(read_text(directory / "SKILL.md"))
        status = catalog.get("status", "active")
        items.append(
            {
                "id": directory.name,
                "version": catalog.get("version"),
                "status": status,
                "status_label": "可用" if status == "active" else "已归档",
                "description": catalog.get("description") or "",
                "keywords": [
                    word for word in catalog.get("keywords") or [] if isinstance(word, str)
                ],
                "body": body,
                "resources": _skill_resources(directory),
            }
        )
    items.sort(key=lambda item: (item["status"] != "active", item["id"]))
    return {
        "items": items,
        "counts": {
            "active": sum(item["status"] == "active" for item in items),
            "total": len(items),
        },
    }
