"""Run-scoped task lifecycle and durable report receipts."""

import hashlib
import json
import threading
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from a3_research_agent.harness.context.manager import atomic
from a3_research_agent.harness.deep_research import TASK_ID
from a3_research_agent.host.research.worker import investigate


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=1200)
    context: str = Field(default="", max_length=4000)
    retry: StrictBool = False


class ResearchTasks:
    """One background investigation at a time per Host."""

    def __init__(self, root, web, papers):
        self.root, self.web, self.papers = root, web, papers
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.running = None

    @property
    def available(self):
        return bool(self.web and self.web.config.enabled and self.web.config.api_key)

    def _store(self, task):
        atomic(self.root / (task["task_id"] + ".json"), json.dumps(task, ensure_ascii=False))

    def _load(self, key):
        if not TASK_ID.fullmatch(key):
            return None
        path = self.root / (key + ".json")
        if not path.is_file():
            return None
        task = json.loads(path.read_text())
        if task["status"] == "running" and self.running != key:
            task.update(status="interrupted", error="research_host_stopped")
            self._store(task)
        return task

    def submit(self, owner, body, model):
        material = {"owner": owner, "question": body.question, "context": body.context}
        key = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:32]
        with self.condition:
            existing = self._load(key)
            if existing and (
                not body.retry or existing["status"] not in {"failed", "incomplete", "interrupted"}
            ):
                return self.public(existing)
            if not self.available or self.stopped.is_set():
                return {
                    "error": "deep_research_unavailable",
                    "hint": "Check the Host's Tavily configuration.",
                }
            if self.running:
                return {
                    "error": "deep_research_busy",
                    "task_id": self.running,
                    "hint": "Collect the running task before starting another.",
                }
            task = {
                **material,
                "task_id": key,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "attempt": existing["attempt"] + 1 if existing else 1,
            }
            self._store(task)
            self.running = key
            threading.Thread(
                target=self._run, args=(task, model), daemon=True, name="deep-research"
            ).start()
            return self.public(task)

    def _run(self, task, model):
        def record(name, result):
            trace = self.root / f"{task['task_id']}-attempt-{task['attempt']}.jsonl"
            with trace.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"tool": name, "result": result}, ensure_ascii=False) + "\n"
                )

        try:
            result = investigate(task, model, self.web, self.papers, self.stopped, record)
        except Exception:  # noqa: BLE001 - keep provider details out of the report
            result = {"status": "failed", "error": "research_execution_failed"}
        with self.condition:
            if self.running != task["task_id"]:
                return
            try:
                self._store(
                    {
                        **task,
                        **result,
                        "ended_at": datetime.now(UTC).isoformat(),
                    }
                )
            finally:
                self.running = None
                self.condition.notify_all()

    def lookup(self, owner, key, wait):
        with self.condition:
            task = self._load(key)
            if task is None or task["owner"] != owner:
                return None
            if task["status"] == "running" and wait:
                self.condition.wait_for(lambda: self.running != key, timeout=wait)
                task = self._load(key)
            return self.public(task)

    def close(self):
        with self.condition:
            self.stopped.set()
            if self.running:
                key = self.running
                task = self._load(key)
                self._store({**task, "status": "interrupted", "error": "research_host_stopped"})
                self.running = None
            self.condition.notify_all()

    @staticmethod
    def public(task):
        return {key: value for key, value in task.items() if key not in {"owner", "context"}}
