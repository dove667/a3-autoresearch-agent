"""Durable research lessons are versioned and retrieved across run boundaries."""

from concurrent.futures import ThreadPoolExecutor

from a3_research_agent.harness.memory import dispatch


def invoke(root, action, run="day-one", **arguments):
    return dispatch({"action": action, **arguments}, root=root, run_id=run)


def test_revisions_archiving_and_run_lifetimes(tmp_path):
    durable = invoke(
        tmp_path, "write", content="Read actual training manifest", refs=["jobs/source"]
    )
    temporary = invoke(tmp_path, "write", content="Wait for current collection", lifetime="run")
    assert temporary["run_ids"] == ["day-one"]
    assert [row["id"] for row in invoke(tmp_path, "list", run="day-two")["items"]] == [
        durable["id"]
    ]
    assert (
        invoke(tmp_path, "read", run="day-two", key=temporary["id"])["error"]
        == "memory_not_applicable"
    )
    assert (
        invoke(tmp_path, "update", id=durable["id"], content="New advice")["error"]
        == "memory_conflict"
    )
    updated = invoke(
        tmp_path,
        "update",
        id=durable["id"],
        revision=durable["revision"],
        content="Check source hashes",
    )
    assert updated["revision"] == 2
    assert (
        invoke(tmp_path, "update", id=durable["id"], revision=1, content="Stale")["error"]
        == "memory_conflict"
    )
    archived = invoke(tmp_path, "archive", id=durable["id"], revision=2, reason="Method superseded")
    assert archived["status"] == "archived"
    assert invoke(tmp_path, "list", run="day-two")["items"] == []
    assert len((tmp_path / "entries.jsonl").read_text().splitlines()) == 4


def test_keyword_normalization_and_pagination(tmp_path):
    invoke(tmp_path, "write", content="2×10 数据去重需要按 seed 和来源核对", scope="DAgger")
    invoke(tmp_path, "write", content="2x10 数据需要实际训练清单", scope="DAgger")
    page = invoke(tmp_path, "search", key="2x10 去重", limit=1)
    assert "去重" in page["items"][0]["preview"]
    assert page["next_offset"] == 1
    second = invoke(tmp_path, "search", key="2x10 去重", limit=1, offset=1)
    assert second["items"][0]["id"] != page["items"][0]["id"]
    assert invoke(tmp_path, "search", key="数据去重")["items"]


def test_semantic_retrieval_cache_and_duplicate_confirmation(tmp_path):
    calls = []

    def embed(texts):
        calls.extend(texts)
        return [
            [1.0, 0.0] if any(word in text for word in ("语义", "semantic", "含义")) else [0.0, 1.0]
            for text in texts
        ]

    def call(action, **arguments):
        return dispatch(
            {"action": action, **arguments},
            root=tmp_path,
            run_id="a",
            embedder=embed,
            model_id="test-v1",
        )

    first = call("write", content="语义检索经验")
    hit = call("search", key="semantic search")
    assert hit["items"][0]["id"] == first["id"]
    assert hit["retrieval"] == "hybrid"
    count = len(calls)
    call("search", key="semantic search")
    assert len(calls) == count
    duplicate = call("write", content="含义相同的另一条记录")
    assert duplicate["error"] == "similar_memory_exists"
    assert (
        call("write", content="含义相同的另一条记录", force=True, reason="Different scope")["id"]
        != first["id"]
    )


def test_updates_are_serialized_and_supersession_is_explicit(tmp_path):
    original = invoke(tmp_path, "write", content="Old count 925")

    def update(value):
        return invoke(tmp_path, "update", id=original["id"], revision=1, content=value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["Count 1000", "Count 1001"]))
    assert sum(result.get("error") == "memory_conflict" for result in results) == 1
    current = invoke(tmp_path, "read", key=original["id"])
    correction = invoke(
        tmp_path, "write", content="Validated count differs", supersedes=original["id"]
    )
    assert [row["id"] for row in invoke(tmp_path, "list")["items"]] == [correction["id"]]
    assert invoke(tmp_path, "read", key=original["id"])["superseded_by"] == [correction["id"]]
    assert current["revision"] == 2


def test_historical_run_notes_are_explicitly_readable_without_becoming_suggestions(tmp_path):
    note = invoke(tmp_path, "write", content="Current job is waiting", lifetime="run")
    audit = invoke(tmp_path, "read", run="next-day", key=note["id"], include_archived=True)
    assert audit["applicable"] is False
    assert invoke(tmp_path, "list", run="next-day")["items"] == []
    page = invoke(tmp_path, "list", run="next-day", include_archived=True)
    assert page["items"][0]["applicable"] is False


def test_document_similarity_detects_duplicates_without_query_instruction_bias(tmp_path):
    class Encoder:
        def __call__(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def query(self, text):
            return [0.0, 1.0]

    encoder = Encoder()
    first = dispatch(
        {"action": "write", "content": "Verify actual training manifest"},
        root=tmp_path,
        run_id="a",
        embedder=encoder,
        model_id="asymmetric",
    )
    duplicate = dispatch(
        {"action": "write", "content": "Check the manifest really used by training"},
        root=tmp_path,
        run_id="b",
        embedder=encoder,
        model_id="asymmetric",
    )
    assert duplicate["error"] == "similar_memory_exists"
    assert duplicate["candidates"][0]["id"] == first["id"]
    correction = dispatch(
        {"action": "write", "content": "Corrected manifest conclusion", "supersedes": first["id"]},
        root=tmp_path,
        run_id="b",
        embedder=encoder,
        model_id="asymmetric",
    )
    assert correction["id"] != first["id"]


def test_long_notes_embed_in_bounded_chunks_and_context_is_progressive(tmp_path):
    sizes = []

    def embed(texts):
        sizes.extend(map(len, texts))
        return [[1.0, 0.0] for _ in texts]

    note = dispatch(
        {
            "action": "write",
            "content": "经验" * 2900,
            "scope": "范围" * 300,
            "refs": ["r" * 600] * 16,
        },
        root=tmp_path,
        run_id="a",
        embedder=embed,
        model_id="bounded",
    )
    view = dispatch(
        {"action": "context", "key": "经验"},
        root=tmp_path,
        run_id="a",
        embedder=embed,
        model_id="bounded",
    )
    assert max(sizes) <= 1200
    assert len(view["items"][0]["preview"]) == 180
    assert len(view["items"][0]["scope"]) <= 180
    assert len(view["items"][0]["refs"][0]) <= 180
    assert len(invoke(tmp_path, "read", key=note["id"])["text"]) == 5800


def test_run_scoped_correction_only_supersedes_within_its_applicable_runs(tmp_path):
    original = invoke(tmp_path, "write", content="Use verified shared checkpoint")
    correction = invoke(
        tmp_path,
        "write",
        content="For this run use the debug checkpoint",
        lifetime="run",
        supersedes=original["id"],
    )
    assert [row["id"] for row in invoke(tmp_path, "list")["items"]] == [correction["id"]]
    next_day = invoke(tmp_path, "list", run="day-two")
    assert [row["id"] for row in next_day["items"]] == [original["id"]]
    assert next_day["items"][0]["superseded_by"] == []
    assert invoke(tmp_path, "read", run="day-two", key=original["id"])["superseded_by"] == []
    invoke(tmp_path, "archive", id=correction["id"], revision=1, reason="Run ended")
    # Archiving the correction does not restore the earlier conclusion within that run.
    assert invoke(tmp_path, "list")["items"] == []
    assert invoke(tmp_path, "list", run="day-two")["items"][0]["id"] == original["id"]


def test_unrelated_semantic_neighbor_is_not_presented_as_experience(tmp_path):
    from a3_research_agent.harness.memory import retrieval

    entry = {
        "id": "note",
        "text": "optimizer failure",
        "scope": "ACT",
        "kind": "lesson",
        "tags": [],
        "refs": [],
    }

    class Encoder:
        def __call__(self, texts):
            return [[0.3, 0.9539392014] for _ in texts]

        def query(self, text):
            return [1.0, 0.0]

    assert (
        retrieval.rank(
            [entry],
            "chocolate cake recipe",
            root=tmp_path,
            embedder=Encoder(),
            model_id="test-negative",
        )
        == []
    )
