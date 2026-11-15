"""Install and verify the reviewed small multilingual embedding model."""

import argparse
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import httpx

MODEL_NAME = "Qwen3-Embedding-0.6B-Q8_0"
MODEL_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
MODEL_BYTES = 639150592
MODEL_REVISION = "a6804f0dece24ece939f52ffa3316fd650ca1839"
MODEL_URL = (
    "https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/"
    + MODEL_REVISION
    + "/Qwen3-Embedding-0.6B-Q8_0.gguf"
)


def model_path(project):
    configured = os.environ.get("A3_EMBEDDING_MODEL")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else project / "model-cache/qwen3-embedding-0.6b/model.gguf"
    )


def verify_model(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            "Embedding model is absent; run the knowledge-service model installer before research"
        )
    if path.stat().st_size != MODEL_BYTES:
        raise ValueError("Embedding model size differs from the reviewed Qwen model")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256:
        raise ValueError("Embedding model SHA256 differs from the reviewed Qwen model")
    return path


def install_model(project):
    target = model_path(Path(project).resolve())
    if target.exists():
        return verify_model(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < MODEL_BYTES + 128 * 1024 * 1024:
        raise OSError("Insufficient free storage for embedding model download")
    descriptor, name = tempfile.mkstemp(prefix=".embedding-download-", dir=target.parent)
    temporary = Path(name)
    try:
        with (
            os.fdopen(descriptor, "wb") as stream,
            httpx.stream(
                "GET", MODEL_URL, follow_redirects=True, timeout=httpx.Timeout(60, read=120)
            ) as response,
        ):
            response.raise_for_status()
            for block in response.iter_bytes(1024 * 1024):
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        verify_model(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    print(install_model(args.project_root))


if __name__ == "__main__":
    main()
