from dataclasses import replace

from a3_research_agent.benchmark import real, status


def test_open_task_becomes_completed_once_and_stable_after_two(tmp_path):
    path = tmp_path / "status.json"
    task = real.TASKS[0]
    state = status.load([task], path)
    assert state["tasks"][task.id]["state"] == "open"

    status.record([task], [{"id": task.id, "passed": True}], path)
    assert status.load([task], path)["tasks"][task.id]["state"] == "completed"
    status.record([task], [{"id": task.id, "passed": True}], path)
    assert status.load([task], path)["tasks"][task.id]["state"] == "stable"
    status.record([task], [{"id": task.id, "passed": False}], path)
    assert status.load([task], path)["tasks"][task.id]["state"] == "open"


def test_changed_criteria_reset_the_state(tmp_path):
    path = tmp_path / "status.json"
    task = real.TASKS[0]
    status.record([task], [{"id": task.id, "passed": True}], path)
    changed = replace(task, criterion=task.criterion + " changed")
    assert status.load([changed], path)["tasks"][changed.id]["state"] == "open"


def test_selection_uses_state(tmp_path):
    path = tmp_path / "status.json"
    first, second = real.TASKS[:2]
    status.record([first, second], [{"id": first.id, "passed": True}], path)
    assert status.selected([first, second], "open", path=path) == [second]
    assert status.selected([first, second], "completed", path=path) == [first]
    assert status.selected([first, second], "all", path=path) == [first, second]
