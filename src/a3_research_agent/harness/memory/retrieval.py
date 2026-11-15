"""Rank applicable research notes using lexical and semantic evidence."""

from a3_research_agent.harness import semantic_index as index


def document(entry):
    return "\n".join([entry["text"], entry["scope"], entry["kind"], " ".join(entry["tags"])])


def rank(entries, query, *, root, embedder=None, model_id="none"):
    if not query.strip():
        return [
            (row, 0.0, None)
            for row in sorted(entries, key=lambda row: (row.get("ts", 0), row["id"]), reverse=True)
        ]
    query_tokens = index.tokens(query)
    normalized_query = index.normalize(query).strip()
    scored = []
    semantic = {}
    if embedder is not None and entries:
        cache = index.VectorCache(root, embedder, model_id)
        query_vector = cache.vectors([query], query=True)[0]
        vectors = cache.vectors([document(entry) for entry in entries])
        semantic = {
            entry["id"]: index.cosine(query_vector, vector)
            for entry, vector in zip(entries, vectors)
        }
    for entry in entries:
        body = index.normalize(document(entry) + " " + " ".join(entry["refs"]))
        overlap = sum(token in body for token in query_tokens) / max(1, len(query_tokens))
        lexical = min(1.0, overlap + (0.25 if normalized_query in body else 0))
        similarity = semantic.get(entry["id"])
        if not lexical and (similarity is None or similarity < 0.35):
            continue
        score = 0.5 * lexical + 0.5 * max(0, similarity) if similarity is not None else lexical
        scored.append((entry, score, similarity))
    return sorted(scored, key=lambda hit: (hit[1], hit[0].get("ts", 0), hit[0]["id"]), reverse=True)
