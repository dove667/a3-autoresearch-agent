"""Encode research passages and instructed queries using a private llama server."""

import hashlib
import math
import threading

import httpx

from .model import MODEL_SHA256

QUERY_INSTRUCTION = "Retrieve research experiences, experimental findings, and failure lessons relevant to the research question."
MODEL_ID = (
    MODEL_SHA256 + ":token3000-v1:" + hashlib.sha256(QUERY_INSTRUCTION.encode()).hexdigest()[:12]
)


class LlamaEmbedder:
    """Use normalized last-token embeddings, serializing small local requests."""

    def __init__(self, endpoint, token):
        self.endpoint = endpoint
        self.token = token
        self.lock = threading.Lock()

    def __call__(self, texts):
        vectors = []
        with (
            self.lock,
            httpx.Client(
                base_url=self.endpoint,
                headers={"Authorization": "Bearer " + self.token},
                timeout=120,
                trust_env=False,
            ) as client,
        ):
            # Small sequential inputs bound unified-memory use independently of corpus size.
            for text in texts:
                tokenized = client.post("/tokenize", json={"content": text, "add_special": True})
                tokenized.raise_for_status()
                tokens = tokenized.json().get("tokens")
                if (
                    not isinstance(tokens, list)
                    or not tokens
                    or any(type(item) is not int for item in tokens)
                ):
                    raise ValueError("Embedding tokenizer returned invalid tokens")
                chunks = [tokens[index : index + 3000] for index in range(0, len(tokens), 3000)]
                pooled = [0.0] * 1024
                for chunk in chunks:
                    response = client.post("/v1/embeddings", json={"input": chunk})
                    response.raise_for_status()
                    rows = response.json().get("data", [])
                    if len(rows) != 1 or not isinstance(rows[0].get("embedding"), list):
                        raise ValueError("Embedding server returned invalid vectors")
                    vector = rows[0]["embedding"]
                    if len(vector) != 1024 or any(
                        not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in vector
                    ):
                        raise ValueError("Embedding server returned invalid dimensions or values")
                    norm = math.sqrt(sum(value * value for value in vector))
                    if norm == 0:
                        raise ValueError("Embedding server returned an empty vector")
                    for index, value in enumerate(vector):
                        pooled[index] += value / norm * len(chunk)
                norm = math.sqrt(sum(value * value for value in pooled))
                if norm == 0:
                    raise ValueError("Embedding chunks produced an empty pooled vector")
                vectors.append([value / norm for value in pooled])
        return vectors

    def query(self, text):
        return self([f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"])[0]
