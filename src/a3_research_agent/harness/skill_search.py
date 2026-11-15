"""Discover skills from metadata and body sections while delivering only summaries."""

import re
from pathlib import Path

from . import semantic_index, skill_library


def metadata(entry):
    return "\n".join([entry["id"], entry["description"], " ".join(entry.get("keywords", []))])


def sections(body):
    """Keep section boundaries useful for retrieval; never inspect attachments here."""
    _, text = skill_library.frontmatter(body.decode("utf-8"))
    parts = re.split(r"(?m)(?=^#{1,6} )", text)
    return [part.strip() for part in parts if part.strip()]


QUERY_FILLERS = {
    "需要",
    "如何",
    "什么",
    "怎么",
    "是否",
    "可以",
    "进行",
    "使用",
    "这个",
    "应该",
    "问题",
    "哪些",
    "哪里",
    "怎样",
    "没有",
    "一直",
    "以及",
    "相关",
    "the",
    "a",
    "an",
    "to",
    "of",
    "and",
    "for",
    "in",
    "is",
    "are",
    "how",
    "what",
    "when",
    "do",
    "does",
    "can",
    "with",
    "on",
    "from",
    "should",
    "need",
}


def lexical_score(query, text):
    terms = semantic_index.tokens(query) - QUERY_FILLERS
    normalized = semantic_index.normalize(text)
    overlap = sum(term in normalized for term in terms) / max(1, len(terms))
    exact = bool(query.strip()) and semantic_index.normalize(query).strip() in normalized
    return min(1.0, overlap + (0.25 if exact else 0.0))


def checked_index_root(directory):
    directory = Path(directory)
    if not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise skill_library.SkillError("invalid_skill_index_root")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "index.sqlite3").is_symlink():
        raise skill_library.SkillError("invalid_skill_index_root")
    return directory


def snapshot(root, history_root, include_archived):
    with skill_library.locked_library(root, history_root):
        return [
            (entry, skill_library.read_body(root, entry))
            for entry in skill_library.catalogs(root)
            if include_archived or entry.get("status") != "archived"
        ]


def search(arguments, *, root, history_root, index_root, embedder=None, model_id="none"):
    """Return current skill metadata; vectors are reused only for identical content."""
    try:
        return _search(
            arguments,
            root=Path(root),
            history_root=Path(history_root),
            index_root=index_root,
            embedder=embedder,
            model_id=model_id,
        )
    except skill_library.SkillError as error:
        return {"error": str(error)}
    except (OSError, ValueError, TypeError, KeyError):
        return {
            "error": "skill_search_unavailable",
            "hint": "Inspect skill metadata or the local semantic service before retrying.",
        }


def _search(arguments, *, root, history_root, index_root, embedder, model_id):
    if not isinstance(arguments, dict) or set(arguments) - {
        "query",
        "offset",
        "limit",
        "include_archived",
    }:
        raise skill_library.SkillError("invalid_skill_search")
    query = arguments.get("query", "")
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 24)
    include_archived = arguments.get("include_archived", False)
    if not isinstance(query, str) or len(query) > 1200:
        raise skill_library.SkillError("invalid_query")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24:
        raise skill_library.SkillError("invalid_page")
    if type(include_archived) is not bool:
        raise skill_library.SkillError("invalid_archived_selection")
    packages = snapshot(root, history_root, include_archived)
    if not query.strip():
        ranked = [(0.0, entry, "catalog") for entry, _ in packages]
    else:
        cache = None
        query_vector = None
        if embedder is not None:
            cache = semantic_index.VectorCache(checked_index_root(index_root), embedder, model_id)
            query_vector = cache.vectors([query], query=True)[0]
        documents = [metadata(entry) for entry, _ in packages]
        metadata_vectors = cache.vectors(documents) if cache and documents else []
        ranked = []
        for index, (entry, body) in enumerate(packages):
            body_sections = sections(body)
            meta_lexical = lexical_score(query, documents[index])
            section_lexical = max(
                (lexical_score(query, section) for section in body_sections), default=0
            )
            meta_semantic = 0.0
            body_semantic = 0.0
            if cache:
                meta_semantic = semantic_index.cosine(query_vector, metadata_vectors[index])
                vectors = cache.vectors(body_sections) if body_sections else []
                body_semantic = max(
                    (semantic_index.cosine(query_vector, vector) for vector in vectors), default=0
                )
            lexical = max(meta_lexical, section_lexical)
            similarity = max(meta_semantic, body_semantic)
            if not lexical and (cache is None or similarity < 0.45):
                continue
            score = 0.55 * lexical + 0.45 * similarity + 0.05 * meta_lexical if cache else lexical
            metadata_match = meta_lexical >= section_lexical and meta_semantic >= body_semantic
            matched_on = "metadata" if metadata_match else "body"
            ranked.append((score, entry, matched_on))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    items = []
    for score, entry, matched_on in ranked[offset : offset + limit]:
        item = {key: entry.get(key) for key in ("id", "version", "status", "description")}
        item["description"] = item["description"][:256]
        item.update(
            score=round(score, 4),
            matched_on=matched_on,
            match_reason={
                "catalog": "Available skill",
                "metadata": "Matched skill purpose and keywords",
                "body": "Matched a method section; read the selected skill to inspect it",
            }[matched_on],
        )
        items.append(item)
    return {
        "skills": items,
        "total": len(ranked),
        "next_offset": offset + limit if offset + limit < len(ranked) else None,
        "retrieval": "hybrid"
        if embedder is not None and query.strip()
        else "lexical"
        if query.strip()
        else "catalog",
    }
