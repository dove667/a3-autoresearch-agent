"""The overview shows the Host's job state, not the Agent's last snapshot."""

from a3_research_agent.host.dashboard.live import JobStatusFeed


class FakeClient:
    def __init__(self, sessions, jobs, fail=False):
        self.sessions = sessions
        self.jobs = jobs
        self.fail = fail
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.fail:
            raise RuntimeError("gateway down")
        payload = self.sessions if path == "/sessions" else self.jobs
        return FakeResponse(payload)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_statuses_come_from_the_gateway_per_session():
    client = FakeClient(
        {"sessions": [{"session_id": "session-a", "closed": False},
                      {"session_id": "session-b", "closed": True}]},
        {"jobs": [{"job_id": "j1", "status": "running", "exit_code": None}]},
    )
    feed = JobStatusFeed(lambda: client)
    assert feed.statuses() == {"j1": {"status": "running", "exit_code": None}}
    assert [path for path, _ in client.calls] == ["/sessions", "/jobs"]


def test_a_failing_gateway_reports_nothing_instead_of_raising():
    feed = JobStatusFeed(lambda: FakeClient({}, {}, fail=True))
    assert feed.statuses() == {}


def test_repeated_reads_use_the_cache_within_the_interval():
    client = FakeClient(
        {"sessions": [{"session_id": "session-a", "closed": False}]},
        {"jobs": [{"job_id": "j1", "status": "queued"}]},
    )
    feed = JobStatusFeed(lambda: client, ttl=60)
    feed.statuses()
    feed.statuses()
    assert len(client.calls) == 2


def test_a_standalone_dashboard_without_a_live_run_reports_nothing(tmp_path):
    feed = JobStatusFeed(root=tmp_path)
    assert feed.statuses("2026-01-01_00-00-00-000000") == {}


def test_each_run_keeps_its_own_cache(tmp_path):
    client = FakeClient(
        {"sessions": [{"session_id": "session-a", "closed": False}]},
        {"jobs": [{"job_id": "j1", "status": "running"}]},
    )
    feed = JobStatusFeed(lambda: client, ttl=60)
    assert feed.statuses("run-a") == {"j1": {"status": "running", "exit_code": None}}
    feed.statuses("run-b")
    assert len(client.calls) == 4


def test_snapshot_overrides_a_stale_recorded_status(tmp_path):
    from a3_research_agent.host.dashboard import snapshot

    run = tmp_path / "trajectories/2026-01-01_00-00-00-000000/conversation"
    run.mkdir(parents=True)
    (run / "operational-state.json").write_text(
        '{"jobs": {"j1": {"status": "queued", "observed_at": 1}}}'
    )
    path = tmp_path / "trajectories/2026-01-01_00-00-00-000000"
    (path / "startup.json").write_text("{}")
    live = {"j1": {"status": "running", "exit_code": None}}
    payload = snapshot.build_snapshot(tmp_path, "latest", job_status=lambda run: live)
    assert payload["agent"]["active_jobs"][0]["status"] == "running"
