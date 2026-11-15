"""Normalize retrieval text and cache model-specific document/query vectors."""

import hashlib
import json
import math
import re
import sqlite3
import unicodedata


def normalize(text):
    return re.sub(r"(?<=\d)\s*[x×✕*]\s*(?=\d)", "x", unicodedata.normalize("NFKC", text).casefold())


def tokens(text):
    text = normalize(text)
    words = re.findall(r"[a-z0-9_-]+", text)
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(span[index : index + 2] for index in range(max(1, len(span) - 1)))
    return set(words)


def cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Embedding dimensions changed; select a different model fingerprint")
    norm = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / norm if norm else 0.0


def encode_documents(embedder, texts):
    chunks, owners = [], []
    for index, text in enumerate(texts):
        parts = [text[start : start + 1200] for start in range(0, max(1, len(text)), 1080)]
        chunks.extend(parts)
        owners.extend([index] * len(parts))
    vectors = list(embedder(chunks))
    if len(vectors) != len(chunks):
        raise ValueError("Embedding response length does not match document chunks")
    pooled = [[] for _ in texts]
    for owner, vector in zip(owners, vectors):
        vector = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in vector))
        pooled[owner].append([value / norm if norm else value for value in vector])
    return [[sum(values) / len(vectors) for values in zip(*vectors)] for vectors in pooled]


class VectorCache:
    def __init__(self, root, embedder, model_id):
        self.path = root / "index.sqlite3"
        self.embedder = embedder
        self.model_id = model_id + ":chunks1200-overlap120-v1"

    def vectors(self, texts, *, query=False):
        keys = [hashlib.sha256((str(query) + "\0" + text).encode()).hexdigest() for text in texts]
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vectors (model TEXT, key TEXT, vector TEXT, PRIMARY KEY(model, key))"
            )
            found = {}
            for key in set(keys):
                row = connection.execute(
                    "SELECT vector FROM vectors WHERE model=? AND key=?", (self.model_id, key)
                ).fetchone()
                if row:
                    found[key] = json.loads(row[0])
            missing = list(dict.fromkeys(key for key in keys if key not in found))
            if missing:
                by_key = dict(zip(keys, texts))
                batch = [by_key[key] for key in missing]
                if query and hasattr(self.embedder, "query"):
                    values = [self.embedder.query(text) for text in batch]
                else:
                    values = (
                        list(self.embedder(batch))
                        if query
                        else encode_documents(self.embedder, batch)
                    )
                if len(values) != len(batch):
                    raise ValueError("Embedding response length does not match input")
                for key, vector in zip(missing, values):
                    vector = [float(value) for value in vector]
                    if not vector or any(not math.isfinite(value) for value in vector):
                        raise ValueError("Embedding contains invalid values")
                    connection.execute(
                        "INSERT OR REPLACE INTO vectors VALUES (?,?,?)",
                        (self.model_id, key, json.dumps(vector)),
                    )
                    found[key] = vector
            return [found[key] for key in keys]
