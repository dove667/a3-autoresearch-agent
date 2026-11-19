import json
import threading

from fastapi.testclient import TestClient

from a3_research_agent.harness import operational_state, plan
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.web.gateway import WebConfig, WebGateway

CODE = "https://github.com/example/policy"
PAPER = "https://arxiv.org/abs/2304.13705"
REPORT = {
    "implementation": [{"url": CODE, "finding": "The config specifies a chunk length."}],
    "papers": [{"url": PAPER, "finding": "Page 4 describes action chunking."}],
    "design": "Hold chunk length fixed when comparing conditioning.",
    "alternatives": ["Shared policy without conditioning", "Shared policy with task conditioning"],
    "limitations": "Validate transfer to the packing scene.",
}


def model_call(name, arguments, key):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": key,
    }


class Papers:
    def call(self, name, arguments):
        if name == "discover_papers":
            return "ACT, arXiv:2304.13705"
        assert arguments["paper"] == "2304.13705"
        return '<paper id="2304.13705"><page num="4">Action chunking</page></paper>'


def test_delegation_is_async_and_report_evidence_can_activate_route(tmp_path):
    unblock = threading.Event()
    web = WebGateway(tmp_path / "web", WebConfig(enabled=True, api_key="stub"))
    web.fetcher = lambda *args: {
        "url": CODE,
        "text": "chunk length = 100",
        "truncated": False,
    }
    web.search = lambda body: [{"url": CODE, "text": "Policy config", "truncated": False}]
    calls = iter(
        [
            [
                model_call("web_search", {"query": "ACT config"}, "a"),
                model_call("paper_search", {"question": "ACT policy", "keywords": ["ACT"]}, "b"),
            ],
            [
                model_call("web_fetch", {"url": CODE}, "c"),
                model_call(
                    "paper_read", {"paper": "2304.13705", "queries": ["action chunking"]}, "d"
                ),
            ],
            [model_call("finish_research", REPORT, "e")],
        ]
    )

    def forward(**request):
        assert unblock.wait(5)
        assert request["model"] == "MiniMax-M3.1-Flash"
        assert all(
            t["name"] not in {"plan", "job_submit", "deep_research"} for t in request["tools"]
        )
        return {"output": next(calls), "status": "completed"}

    app = create_app(
        model_name="MiniMax-M3.1-Flash",
        main_owner="main",
        sessions={"owner": "main", "other": "other"},
        forward_model=forward,
        remote_call=lambda *_: None,
        usage_log=tmp_path / "usage.jsonl",
        web_gateway=web,
        paper_gateway=Papers(),
    )
    auth = {"Authorization": "Bearer owner"}
    with TestClient(app) as client:
        body = {"question": "Does task conditioning help?", "context": "Compare ACT variants."}
        res = client.post("/deep-research", headers=auth, json=body)
        task = res.json()
        assert res.status_code == 200 and task["status"] == "running", task
        assert (
            client.post("/deep-research", headers=auth, json=body).json()["task_id"]
            == task["task_id"]
        )
        path = "/deep-research/" + task["task_id"]
        assert client.get(path, headers={"Authorization": "Bearer other"}).status_code == 403
        unblock.set()
        result = client.get(path, headers=auth, params={"wait_seconds": 5}).json()
        assert result["status"] == "completed", result
        assert result["report"] == REPORT
        assert set(result["sources"]) == {CODE, PAPER}
        operational_state.record_gateway("GET", path, result)
        route = {
            "id": "conditioned-act",
            "status": "active",
            "question": body["question"],
            "hypothesis": "Task labels help.",
            "experiment": "Compare shared policies.",
            "decision_rule": "Use paired seeds.",
            "research": {k: REPORT[k] for k in ("implementation", "papers", "design")},
        }
        assert (
            plan.dispatch(
                {
                    "action": "update",
                    "revision": 0,
                    "direction": {"objective": "Improve A3"},
                    "routes": [route],
                },
                require_research=True,
            )["revision"]
            == 1
        )
    assert (
        json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])["model_purpose"]
        == "research"
    )


def test_alpha_xiv_reads_only_the_configured_environment_token(tmp_path, monkeypatch):
    from a3_research_agent.host.research.papers import AlphaXiv

    monkeypatch.delenv("A3_ALPHAXIV_TOKEN", raising=False)
    monkeypatch.setenv("A3_ALPHAXIV_CC_SWITCH_DB", str(tmp_path / "unused"))
    from unittest.mock import patch

    with patch("sqlite3.connect", side_effect=AssertionError("unexpected database access")):
        assert AlphaXiv.from_env() is None
        monkeypatch.setenv("A3_ALPHAXIV_TOKEN", "  fixture-token  ")
        client = AlphaXiv.from_env()
        assert client.token == "fixture-token"
        assert "fixture-token" not in repr(client)


def test_unread_sources_are_rejected_and_missing_lanes_remain_incomplete():
    import pytest

    from a3_research_agent.host.research.worker import checked_report

    sources = {"implementation": {CODE}, "papers": set()}
    with pytest.raises(ValueError, match="exact URL"):
        checked_report(REPORT, sources)
    status, urls = checked_report(REPORT | {"papers": []}, sources)
    assert status == "incomplete"
    assert urls == [CODE]
    assert (
        checked_report(
            REPORT
            | {"design": "Detailed recommendation. " * 200, "limitations": "Unknown. " * 300},
            sources | {"papers": {PAPER}},
        )[0]
        == "completed"
    )


def test_interrupted_research_is_reported_on_restart_without_resubmitting(tmp_path):
    from a3_research_agent.host.research.tasks import ResearchTasks

    task_id = "a" * 32
    (tmp_path / f"{task_id}.json").write_text(
        json.dumps(
            {
                "owner": "main",
                "task_id": task_id,
                "status": "running",
            }
        )
    )
    tasks = ResearchTasks(tmp_path, None, None)
    task = tasks.lookup("main", task_id, 0)
    assert task["status"] == "interrupted"
    assert json.loads((tmp_path / f"{task_id}.json").read_text())["status"] == "interrupted"
