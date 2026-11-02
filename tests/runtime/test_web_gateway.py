import json
import socket

import pytest
from fastapi.testclient import TestClient

from runtime.local import web_gateway as web
from runtime.local.host_gateway import create_app


def service(tmp_path, **kwargs):
    return web.WebGateway(
        tmp_path / ".host" / "web", web.WebConfig(enabled=True, api_key="secret-key", **kwargs)
    )


def client(tmp_path, svc=None):
    return TestClient(
        create_app(
            model_name="test",
            sessions={"one": "alice", "two": "bob"},
            forward_model=lambda **kw: {},
            remote_call=lambda *a: {},
            usage_log=tmp_path / "usage.jsonl",
            web_gateway=svc,
        )
    )


def test_auth_disabled_and_missing_key(tmp_path):
    c = client(tmp_path)
    assert c.get("/web/status").status_code == 401
    assert c.get("/web/status", headers={"Authorization": "Bearer one"}).json()["enabled"] is False
    assert (
        c.post("/web/search", json={"query": "x"}, headers={"Authorization": "Bearer one"}).json()[
            "detail"
        ]["code"]
        == "disabled"
    )
    svc = web.WebGateway(tmp_path / "web", web.WebConfig(enabled=True))
    c = client(tmp_path, svc)
    assert (
        c.get("/web/status", headers={"Authorization": "Bearer one"}).json()["configured"] is False
    )
    assert (
        c.post("/web/search", json={"query": "x"}, headers={"Authorization": "Bearer one"}).json()[
            "detail"
        ]["code"]
        == "unconfigured"
    )


def test_search_fixed_config_normalization_and_restart_accounting(tmp_path, monkeypatch):
    svc = service(tmp_path)
    seen = []
    monkeypatch.setattr(
        web,
        "resolve_public_url",
        lambda url: web.PublicTarget(
            url, "api.tavily.com", "93.184.216.34", 443, "https", "/search"
        ),
    )

    def factory(target, timeout):
        conn = FakeConnection(
            FakeResponse(
                json.dumps(
                    {
                        "results": [
                            {
                                "title": "Paper",
                                "url": "https://example.org",
                                "content": "x" * 17000,
                                "published_date": "bad",
                            }
                        ]
                    }
                ).encode()
            )
        )
        seen.append((target, conn))
        return conn

    svc.search_connection_factory = factory
    c = client(tmp_path, svc)
    r = c.post(
        "/web/search",
        json={"query": "public paper", "domains": ["example.org"]},
        headers={"Authorization": "Bearer one"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["request_id"] == r.headers["X-Request-ID"]
    assert data["sources"][0]["published_at"] is None
    assert len(data["sources"][0]["text"]) == 16000 and data["truncated"] and data["untrusted"]
    body = json.loads(seen[0][1].requests[0][1]["body"])
    assert seen[0][0].url == "https://api.tavily.com/search"
    assert seen[0][1].requests[0][1]["headers"]["Authorization"] == "Bearer secret-key"
    assert body == {
        "query": "public paper",
        "max_results": 5,
        "include_domains": ["example.org"],
        "topic": "general",
        "search_depth": "basic",
        "auto_parameters": False,
        "include_answer": False,
        "include_raw_content": False,
    }
    resumed = service(tmp_path)
    resumed.search_connection_factory = factory
    restarted = client(tmp_path, resumed)
    r = restarted.post(
        "/web/search", json={"query": "another"}, headers={"Authorization": "Bearer two"}
    )
    assert r.status_code == 200
    assert resumed.budget()["search"] == {"limit": None, "used": 2}
    assert "secret-key" not in repr(svc.config)
    logs = (tmp_path / ".host/web/events.jsonl").read_text()
    assert "public paper" not in logs and "secret-key" not in logs and "alice" in logs


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "x" * 1001},
        {"query": "x", "max_results": 11},
        {"query": "x", "api_key": "evil"},
        {"query": "x", "domains": ["http://localhost"]},
        {"query": "x", "max_results": True},
    ],
)
def test_invalid_search(tmp_path, payload):
    c = client(tmp_path, service(tmp_path))
    assert (
        c.post("/web/search", json=payload, headers={"Authorization": "Bearer one"}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://169.254.169.254",
        "http://10.0.0.1",
        "http://user:pass@example.org",
        "http://example.org:8080",
        "file:///etc/passwd",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(web.WebError):
        web.resolve_public_url(
            url, lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        )


def test_dns_all_addresses_and_pin():
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    target = web.resolve_public_url("https://example.org/page#anchor", lambda *a: records)
    assert target.ip == "93.184.216.34" and target.host == "example.org" and target.path == "/page"
    with pytest.raises(web.WebError):
        web.resolve_public_url(
            "https://example.org",
            lambda *a: records + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
        )


class FakeResponse:
    def __init__(self, data=b"hello", status=200, headers=None):
        self.data, self.status = data, status
        self.headers = headers or {"Content-Type": "text/plain"}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, count):
        data, self.data = self.data[:count], self.data[count:]
        return data


class FakeConnection:
    def __init__(self, response):
        self.response, self.sock, self.closed = response, None, False
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def finish(self):
        self.closed = True


def public_dns(*a):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_fetch_html_redirect_limits_and_identity():
    connections = []

    def factory(target, timeout):
        response = (
            FakeResponse(status=302, headers={"Location": "/next"})
            if not connections
            else FakeResponse(
                b"<title>Test</title><script>EVIL</script><style>BAD</style><p>Good content</p>",
                headers={"Content-Type": "text/html"},
            )
        )
        conn = FakeConnection(response)
        connections.append((target, conn))
        return conn

    result = web.fetch_public(
        "https://example.org/start", 8, resolver=public_dns, connection_factory=factory
    )
    assert result["text"] == "Test\nGoo" and result["truncated"]
    assert result["url"] == "https://example.org/next" and result["title"] == "Test"
    assert all(c.closed for _, c in connections)
    assert connections[0][1].requests[0][1]["headers"]["Accept-Encoding"] == "identity"
    assert connections[0][1].requests[0][1]["headers"]["Host"] == "example.org"


@pytest.mark.parametrize(
    "headers,data,code",
    [
        ({"Content-Type": "application/pdf"}, b"pdf", "unsupported_content"),
        ({"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"x", "unsupported_encoding"),
        (
            {"Content-Type": "text/plain", "Content-Length": str(web.MAX_BYTES + 1)},
            b"x",
            "response_too_large",
        ),
        ({"Content-Type": "text/plain"}, b"x" * (web.MAX_BYTES + 1), "response_too_large"),
    ],
)
def test_fetch_content_limits(headers, data, code):
    with pytest.raises(web.WebError, match=code):
        web.fetch_public(
            "https://example.org",
            16000,
            resolver=public_dns,
            connection_factory=lambda *a: FakeConnection(FakeResponse(data, headers=headers)),
        )


def test_redirect_revalidates_private_and_rebinding():
    calls = []

    def dns(*args):
        calls.append(args)
        return (
            public_dns()
            if len(calls) == 1
            else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        )

    connections = []

    def factory(target, *args):
        connections.append(target)
        return FakeConnection(FakeResponse(status=302, headers={"Location": "/again"}))

    with pytest.raises(web.WebError, match="unsafe_url"):
        web.fetch_public("https://example.org", 100, resolver=dns, connection_factory=factory)
    assert len(connections) == 1


def test_actual_connection_pins_ip_tls_and_deadline(monkeypatch):
    calls = []

    class Sock:
        def settimeout(self, value):
            calls.append(("timeout", value))

        def connect(self, target):
            calls.append(("connect", target))

        def close(self):
            calls.append(("close",))

        def shutdown(self, how):
            calls.append(("shutdown", how))

        def do_handshake(self):
            calls.append(("handshake",))

    sock = Sock()
    monkeypatch.setattr(web.socket, "socket", lambda *args: sock)

    class Context:
        def wrap_socket(self, raw, server_hostname, do_handshake_on_connect):
            assert do_handshake_on_connect is False
            calls.append(("tls", server_hostname))
            return raw

    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    target = web.resolve_public_url("https://example.org", public_dns)
    connection = web.PinnedConnection(target, 0.02)
    connection.connect()
    import time

    time.sleep(0.04)
    connection.finish()
    assert ("connect", ("93.184.216.34", 443)) in calls
    assert ("tls", "example.org") in calls
    assert ("shutdown", socket.SHUT_RDWR) in calls


def test_connection_close_header_does_not_cancel_deadline(monkeypatch):
    import io
    import time

    calls = []

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            pass

        def sendall(self, data):
            pass

        def makefile(self, *args):
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\nbody"
            )

        def close(self):
            calls.append("close")

        def shutdown(self, how):
            calls.append("shutdown")

    monkeypatch.setattr(web.socket, "socket", lambda *args: Sock())
    target = web.resolve_public_url("http://example.org", public_dns)
    connection = web.PinnedConnection(target, 0.02)
    connection.request("GET", "/")
    response = connection.getresponse()
    assert response.will_close
    assert connection.timer.is_alive()
    time.sleep(0.04)
    assert "shutdown" in calls
    connection.finish()


def test_failed_requests_are_counted_and_redacted(tmp_path):
    svc = service(tmp_path)

    def broken(*args):
        raise RuntimeError("secret-key and private-query")

    svc.fetcher = broken
    c = client(tmp_path, svc)
    r = c.post(
        "/web/fetch",
        json={"url": "https://example.org/private-query"},
        headers={"Authorization": "Bearer one"},
    )
    assert r.status_code == 502 and r.json()["detail"]["code"] == "upstream_error"
    assert "secret-key" not in r.text and "private-query" not in r.text
    r = c.post(
        "/web/fetch", json={"url": "https://example.org"}, headers={"Authorization": "Bearer two"}
    )
    assert r.status_code == 502
    assert svc.budget()["fetch"]["used"] == 2
    logs = (svc.root / "events.jsonl").read_text()
    assert "secret-key" not in logs and "private-query" not in logs
    assert json.loads(logs.splitlines()[0])["budget_reserved"] is True


def test_empty_search_timeout_busy_shutdown(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.search = lambda body: []
    c = client(tmp_path, svc)
    headers = {"Authorization": "Bearer one"}
    assert c.post("/web/search", json={"query": "x"}, headers=headers).json()["sources"] == []

    def timeout(body):
        raise TimeoutError("secret-key")

    svc.search = timeout
    assert (
        c.post("/web/search", json={"query": "x", "refresh": True}, headers=headers).json()[
            "detail"
        ]["code"]
        == "timeout"
    )
    svc.parallel.acquire()
    svc.parallel.acquire()
    assert (
        c.post("/web/search", json={"query": "x"}, headers=headers).json()["detail"]["code"]
        == "busy"
    )
    assert svc.status()["budget"]["search"]["used"] == 2
    svc.parallel.release()
    svc.parallel.release()
    c.app.state.begin_shutdown()
    assert c.post("/web/search", json={"query": "x"}, headers=headers).status_code == 503
    assert c.get("/web/status", headers=headers).status_code == 503


def test_budget_corruption_fails_closed_and_resume_does_not_reset(tmp_path):
    svc = service(tmp_path)
    svc.budget("search")
    restarted = service(tmp_path)
    assert restarted.budget()["search"] == {"limit": None, "used": 1}
    (svc.root / "budget.json").write_text("{bad")
    with pytest.raises(web.WebError, match="budget_unavailable"):
        restarted.budget("search")


def test_atomic_budget_across_service_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    services = [service(tmp_path) for _ in range(8)]

    def reserve(svc):
        try:
            svc.budget("search")
            return True
        except web.WebError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, services)) == 8
    assert services[0].budget()["search"]["used"] == 8


@pytest.mark.parametrize(
    "url", ["http://[64:ff9b::7f00:1]", "http://[2002:7f00:1::]", "http://[2001::1]"]
)
def test_no_ipv6_translation_tunnels(url):
    with pytest.raises(web.WebError, match="unsafe_url"):
        web.resolve_public_url(url)


def test_host_environment_config_redacts_key(monkeypatch):
    monkeypatch.setenv("A3_WEB_ENABLED", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "super-secret")
    monkeypatch.setenv("A3_WEB_SEARCH_LIMIT", "12")
    config = web.WebConfig.from_env()
    assert config.enabled
    assert not hasattr(config, "search_limit") and not hasattr(config, "fetch_limit")
    assert "super-secret" not in repr(config)
    monkeypatch.setenv("A3_WEB_FETCH_LIMIT", "-1")
    assert web.WebConfig.from_env() == config
    monkeypatch.setenv("A3_WEB_SEARCH_LIMIT", "not-an-integer")
    assert web.WebConfig.from_env() == config


def test_redirect_count_is_bounded():
    seen = []

    def factory(target, timeout):
        seen.append(target)
        return FakeConnection(FakeResponse(status=302, headers={"Location": "/loop"}))

    with pytest.raises(web.WebError, match="redirect_limit"):
        web.fetch_public(
            "https://example.org", 100, resolver=public_dns, connection_factory=factory
        )
    assert len(seen) == 4


def test_tls_verification_failure_never_returns_content(monkeypatch):
    import ssl

    closed = []

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            pass

        def close(self):
            closed.append(True)

    class Context:
        def wrap_socket(self, *args, **kwargs):
            raise ssl.SSLCertVerificationError("untrusted certificate")

    monkeypatch.setattr(web.socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    connection = web.PinnedConnection(web.resolve_public_url("https://example.org", public_dns), 1)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            connection.connect()
    finally:
        connection.finish()
    assert closed


def test_audit_records_budget_snapshot(tmp_path):
    svc = service(tmp_path)
    svc.search = lambda body: []
    svc.execute("search", web.SearchRequest(query="public"), "alice")
    event = json.loads((svc.root / "events.jsonl").read_text())
    assert event["budget"]["search"] == {"limit": None, "used": 1}


def test_deadline_interrupts_blocked_http_headers(monkeypatch):
    """Exercise the actual HTTP parser, not only a standalone timer callback."""
    import threading

    interrupted = threading.Event()

    class BlockedHeaders:
        def flush(self):
            pass

        def readline(self, limit=-1):
            if not interrupted.wait(2):
                pytest.fail("absolute deadline did not interrupt a blocked header read")
            raise OSError("socket was shut down")

        def close(self):
            pass

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            pass

        def sendall(self, data):
            pass

        def makefile(self, *args):
            return BlockedHeaders()

        def shutdown(self, how):
            interrupted.set()

        def close(self):
            pass

    monkeypatch.setattr(web.socket, "socket", lambda *args: Sock())
    connection = web.PinnedConnection(web.resolve_public_url("http://example.org", public_dns), 0.1)
    try:
        connection.request("GET", "/")
        with pytest.raises(OSError, match="socket was shut down"):
            connection.getresponse()
        assert interrupted.is_set()
    finally:
        connection.finish()


@pytest.mark.parametrize("operation,count", [("search", 101), ("fetch", 201)])
def test_no_cumulative_run_cap(tmp_path, operation, count):
    svc = service(tmp_path)
    svc.search = lambda body: []
    svc.fetcher = lambda *args: {"text": "public", "truncated": False}
    c = client(tmp_path, svc)
    body = {"query": "public"} if operation == "search" else {"url": "https://example.org"}
    for i in range(count):
        response = c.post(
            f"/web/{operation}",
            json=body,
            headers={"Authorization": "Bearer " + ("one" if i % 2 else "two")},
        )
        assert response.status_code == 200
    state = service(tmp_path).status()
    assert state["no_run_cap"] is True
    assert state["budget"][operation] == {"limit": None, "used": count}


def test_legacy_limits_migrate_without_resetting_usage_or_history(tmp_path):
    svc = service(tmp_path)
    svc.root.mkdir(parents=True, exist_ok=True)
    old = {"search": {"limit": 100, "used": 100}, "fetch": {"limit": 200, "used": 200}}
    (svc.root / "budget.json").write_text(json.dumps(old))
    history = '{"historical": "unchanged"}\n'
    (svc.root / "events.jsonl").write_text(history)
    assert svc.budget()["search"] == {"limit": None, "used": 100}
    assert json.loads((svc.root / "budget.json").read_text())["fetch"] == {
        "limit": None,
        "used": 200,
    }
    assert service(tmp_path).budget("search")["search"]["used"] == 101
    assert service(tmp_path).budget("fetch")["fetch"]["used"] == 201
    assert (svc.root / "events.jsonl").read_text() == history


@pytest.mark.parametrize("used", [-1, True, "100", None])
def test_invalid_accounting_fails_closed_without_overwrite(tmp_path, used):
    svc = service(tmp_path)
    svc.root.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"search": {"limit": 100, "used": used}, "fetch": {"limit": 200, "used": 0}})
    (svc.root / "budget.json").write_text(raw)
    with pytest.raises(web.WebError, match="budget_unavailable"):
        svc.budget("search")
    assert (svc.root / "budget.json").read_text() == raw


@pytest.mark.parametrize(
    "status,code,http_status",
    [
        (401, "provider_auth_error", 502),
        (429, "provider_rate_limited", 429),
        (432, "provider_quota_exhausted", 429),
        (433, "provider_quota_exhausted", 429),
        (434, "provider_error", 502),
        (500, "provider_error", 502),
    ],
)
def test_provider_failures_are_distinct_redacted_and_not_retried(
    tmp_path, monkeypatch, status, code, http_status
):
    svc = service(tmp_path)
    monkeypatch.setattr(
        web,
        "resolve_public_url",
        lambda url: web.PublicTarget(
            url, "api.tavily.com", "93.184.216.34", 443, "https", "/search"
        ),
    )
    connections = []

    def factory(*args):
        conn = FakeConnection(FakeResponse(b"secret-key private-query raw-error", status=status))
        connections.append(conn)
        return conn

    svc.search_connection_factory = factory
    r = client(tmp_path, svc).post(
        "/web/search", json={"query": "private-query"}, headers={"Authorization": "Bearer one"}
    )
    assert r.status_code == http_status
    assert r.json()["detail"]["code"] == code
    assert r.json()["detail"]["request_id"] == r.headers["X-Request-ID"]
    assert len(connections) == 1 and connections[0].closed
    logs = (svc.root / "events.jsonl").read_text()
    assert json.loads(logs)["status"] == code
    assert svc.budget()["search"]["used"] == 1
    for secret in ("secret-key", "private-query", "raw-error"):
        assert secret not in r.text + logs
