"""The state view names the next move instead of leaving it to inference."""

import pytest

from a3_research_agent.harness import plan as planning


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    path = tmp_path / "plan.json"
    monkeypatch.setenv("A3_PLAN_FILE", str(path))
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "state"))
    planning.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {"objective": "提高 2×5 与 2×10 入盒准确率"},
            "routes": [
                {
                    "id": "r1",
                    "question": "扩采数据能否提升 2×10",
                    "hypothesis": "更多合格数据提高入盒数",
                    "experiment": "同种子配对评测",
                    "decision_rule": "配对改善则保留",
                }
            ],
            "steps": [
                {
                    "id": "s1",
                    "route": "r1",
                    "task": "同种子配对比较新旧 checkpoint",
                    "criterion": "逐种子报告入盒数与原生通过",
                },
                {"id": "s2", "route": "r1", "task": "整理失败回合", "criterion": "写出失败分类"},
            ],
            "reason": "start",
        }
    )
    return path


def test_ready_step_is_named_with_its_criterion(seeded):
    text = planning.view(planning.load(), jobs={}, compact=True)["next_action"]
    assert "s1" in text and "逐种子报告入盒数" in text
    assert text.endswith("。")


def test_a_finished_job_asks_for_a_review_first(seeded):
    jobs = {"a" * 32: {"status": "completed"}}
    plan = planning.load()
    planning.dispatch(
        {
            "action": "update",
            "revision": plan["revision"],
            "steps": [{"id": "s1", "jobs": ["a" * 32]}],
            "reason": "绑定作业",
        }
    )
    text = planning.view(planning.load(), jobs=jobs, compact=True)["next_action"]
    assert "复盘" in text and "metrics" in text


def test_a_running_job_is_waited_on_rather_than_resubmitted(seeded):
    jobs = {"b" * 32: {"status": "running"}}
    plan = planning.load()
    planning.dispatch(
        {
            "action": "update",
            "revision": plan["revision"],
            "steps": [{"id": "s1", "jobs": ["b" * 32], "status": "active"}],
            "reason": "作业在跑",
        }
    )
    text = planning.view(planning.load(), jobs=jobs, compact=True)["next_action"]
    assert "不要重复提交" in text


def test_a_blocked_plan_asks_for_a_new_action_not_more_checking(seeded):
    plan = planning.load()
    planning.dispatch(
        {
            "action": "update",
            "revision": plan["revision"],
            "steps": [
                {"id": "s1", "status": "blocked", "progress": "缺 2×10 权重"},
                {"id": "s2", "status": "blocked", "progress": "等待 s1"},
            ],
            "reason": "都卡住",
        }
    )
    text = planning.view(planning.load(), jobs={}, compact=True)["next_action"]
    assert "新增" in text or "调整" in text


def test_an_empty_plan_still_tells_the_model_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_PLAN_FILE", str(tmp_path / "empty.json"))
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "state"))
    text = planning.view(planning.load(), jobs={}, compact=True)["next_action"]
    assert "plan update" in text
