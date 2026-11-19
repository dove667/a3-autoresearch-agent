import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.execution.controller.remote_assets import inspect_assets
from a3_research_agent.harness import agent, tool_catalog
from a3_research_agent.host.gateway.app import create_app


@pytest.fixture
def assets(tmp_path):
    settings = SimpleNamespace(
        data_root=tmp_path,
        datasets_root=tmp_path / "DATASETS",
        models_root=tmp_path / "MODELS",
    )
    settings.datasets_root.mkdir()
    settings.models_root.mkdir()
    folder = settings.datasets_root / "shared"
    folder.mkdir()
    (folder / "meta.json").write_text("零一二三", encoding="utf-8")
    (folder / "binary").write_bytes(b"\xff")
    (settings.datasets_root / "a3-migration.json").write_text(
        json.dumps({"assets": [{"path": "shared/meta.json", "source": "/old/root/one"}]})
    )
    return settings


def test_remote_assets_supports_bounded_list_stat_search_and_text(assets):
    def lookup(action, **args):
        return inspect_assets({"action": action, "root": "datasets", **args}, assets)

    listing = lookup("list", path="shared", limit=1)
    assert listing["entries"] == [{"name": "binary", "type": "file"}]
    assert listing["next_offset"] == 1
    assert lookup("list", path="shared", offset=1)["entries"][0]["name"] == "meta.json"
    assert lookup("stat", path="shared/meta.json")["bytes"] == 12
    assert lookup("search", query="ROOT/one")["matches"][0]["path"] == "shared/meta.json"
    assert lookup("read", path="shared/meta.json", offset=1, limit=2)["content"] == "一二"
    assert lookup("read", path="shared/binary")["error"] == "asset_not_text"


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "shared/../secret", ".env", "shared//x"]
)
def test_remote_assets_rejects_noncanonical_paths(assets, path):
    assert (
        inspect_assets({"action": "stat", "root": "datasets", "path": path}, assets)["error"]
        == "invalid_asset_request"
    )


def test_remote_assets_does_not_traverse_links_or_open_special_files(assets):
    private = assets.data_root / "private"
    private.mkdir()
    (private / "secret").write_text("private")
    (assets.datasets_root / "link").symlink_to(private)
    os.mkfifo(assets.datasets_root / "pipe")
    for path in ("link/secret", "pipe"):
        result = inspect_assets({"action": "read", "root": "datasets", "path": path}, assets)
        assert result["error"] == "asset_unavailable"
    assert (
        inspect_assets({"action": "stat", "root": "datasets", "path": "link"}, assets)["type"]
        == "symlink"
    )
    assets.models_root = private
    assert (
        inspect_assets({"action": "list", "root": "workspace"}, assets)["error"]
        == "invalid_asset_request"
    )


def test_remote_assets_gateway_is_authenticated_and_callable_from_agent(
    assets, tmp_path, monkeypatch
):
    calls = []

    def remote(operation, payload):
        calls.append(operation)
        return inspect_assets(payload, assets)

    app = create_app(
        model_name="test",
        sessions={"token": "owner"},
        remote_call=remote,
        forward_model=lambda **_: None,
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:
        args = {"action": "search", "root": "datasets", "query": "meta"}
        assert client.post("/remote-assets", json=args).status_code == 401
        assert calls == []

        def request(method, path, data=None, **_):
            response = client.request(
                method, path, json=data, headers={"Authorization": "Bearer token"}
            )
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(agent, "req", request)
        monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
        assert "remote_assets" in tool_catalog.BY_NAME
        result = agent.tool("remote_assets", args)
        assert result["matches"][0]["path"] == "shared/meta.json"
        assert calls == ["remote-assets"]
