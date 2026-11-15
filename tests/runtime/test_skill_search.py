"""Skill discovery keeps bodies private while matching methods across languages."""

from fastapi.testclient import TestClient

from a3_research_agent.harness import skill_registry, skill_search
from a3_research_agent.host.knowledge_service.worker import create_knowledge_app


def save(project, skill_id, *, body, description="Method", **fields):
    return skill_registry.dispatch(
        {
            "action": "save",
            "id": skill_id,
            "content": body,
            "description": description,
            "source": "test-method",
            **fields,
        },
        root=project / "skills",
        history_root=project / ".runtime/skill-revisions",
    )


def search(project, query, **kwargs):
    return skill_search.search(
        {"query": query, **kwargs},
        root=project / "skills",
        history_root=project / ".runtime/skill-revisions",
        index_root=project / ".runtime/skill-index",
    )


def test_body_keywords_and_chinese_match_without_disclosing_methods_or_attachments(tmp_path):
    save(
        tmp_path,
        "frame-cache",
        description="Cache repeated video decoding",
        body="# Procedure\n帧缓存可减少视频重复解码。\nUse preflight to validate. SECRET BODY DETAIL",
    )
    resource = tmp_path / "skills/frame-cache/references/private.txt"
    resource.parent.mkdir()
    resource.write_text("zyxxattachment999")
    for query in ("帧缓存", "preflight"):
        result = search(tmp_path, query)
        assert result["skills"][0]["id"] == "frame-cache"
        assert result["skills"][0]["matched_on"] == "body"
        assert "SECRET" not in str(result)
        assert "content" not in result["skills"][0]
        assert "resources" not in result["skills"][0]
    assert search(tmp_path, "zyxxattachment999")["skills"] == []


def test_semantic_discovery_reuses_cache_and_refreshes_after_body_edits(tmp_path):
    saved = save(
        tmp_path,
        "training-progress",
        description="Inspect stalled training logs",
        body="# Training\nCheck whether the training log advances.",
    )
    calls = []

    class Encoder:
        def __call__(self, texts):
            calls.extend(texts)
            return [[1.0, 0.0] for _ in texts]

        def query(self, text):
            calls.append(text)
            return [1.0, 0.0]

    def semantic_search():
        return skill_search.search(
            {"query": "训练日志一直不更新"},
            root=tmp_path / "skills",
            history_root=tmp_path / ".runtime/skill-revisions",
            index_root=tmp_path / ".runtime/skill-index",
            embedder=Encoder(),
            model_id="test-model",
        )

    result = semantic_search()
    assert result["skills"][0]["id"] == "training-progress"
    assert result["retrieval"] == "hybrid"
    count = len(calls)
    assert semantic_search() == result and len(calls) == count
    changed = save(
        tmp_path,
        "training-progress",
        description="Inspect stalled training logs",
        body="# Training\nA changed method checks the run ledger.",
        expected_sha256=saved["sha256"],
    )
    result = semantic_search()
    assert len(calls) > count and result["skills"][0]["version"] == changed["version"]
    assert (tmp_path / ".runtime/skill-index/index.sqlite3").is_file()
    assert not (tmp_path / "memory/index.sqlite3").exists()


def test_catalog_pagination_and_archives_use_current_metadata(tmp_path):
    first = save(tmp_path, "first", body="olduniquetoken")
    save(tmp_path, "second", body="anotherunique", description="Another method")
    page = search(tmp_path, "", limit=1)
    assert page["total"] == 2 and page["next_offset"] == 1
    assert search(tmp_path, "", offset=1, limit=1)["skills"][0]["id"] == "second"
    changed = save(tmp_path, "first", body="newuniquetoken", expected_sha256=first["sha256"])
    assert not search(tmp_path, "olduniquetoken")["skills"]
    assert search(tmp_path, "newuniquetoken")["skills"]
    skill_registry.dispatch(
        {"action": "archive", "id": "first", "expected_sha256": changed["sha256"]},
        root=tmp_path / "skills",
        history_root=tmp_path / ".runtime/skill-revisions",
    )
    assert not search(tmp_path, "newuniquetoken")["skills"]
    archived = search(tmp_path, "newuniquetoken", include_archived=True)["skills"][0]
    assert archived["status"] == "archived"


def test_shared_service_authenticates_skill_queries_and_returns_only_summaries(tmp_path):
    save(tmp_path, "preflight", body="CPU预检方法 SECRET METHOD")
    with TestClient(create_knowledge_app(tmp_path, "run", "private", None)) as client:
        assert client.post("/skills/search", json={"query": "CPU预检"}).status_code == 401
        client.headers["Authorization"] = "Bearer private"
        result = client.post("/skills/search", json={"query": "CPU预检"}).json()
        assert result["skills"][0]["id"] == "preflight"
        assert "SECRET" not in str(result)


def test_index_rejects_symlink_to_other_storage(tmp_path):
    save(tmp_path, "method", body="Method")
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    index_root = tmp_path / ".runtime/skill-index"
    index_root.symlink_to(destination)
    result = skill_search.search(
        {"query": "method"},
        root=tmp_path / "skills",
        history_root=tmp_path / ".runtime/skill-revisions",
        index_root=index_root,
        embedder=lambda texts: [[1.0, 0.0] for _ in texts],
    )
    assert result["error"] == "invalid_skill_index_root"
    assert not list(destination.iterdir())


def test_generic_query_words_do_not_retrieve_unrelated_methods(tmp_path):
    save(tmp_path, "train", description="训练步骤", body="需要先检查训练状态。")

    class Encoder:
        def __call__(self, texts):
            return [[0.4, 0.916515139] for _ in texts]

        def query(self, text):
            return [1.0, 0.0]

    result = skill_search.search(
        {"query": "烤巧克力蛋糕需要多少温度"},
        root=tmp_path / "skills",
        history_root=tmp_path / ".runtime/skill-revisions",
        index_root=tmp_path / ".runtime/skill-index",
        embedder=Encoder(),
        model_id="negative",
    )
    assert result["skills"] == []
