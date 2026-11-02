import io
import json
import ssl

import pytest

from runtime.local import web_gateway as web
from tests.runtime.test_web_gateway import FakeConnection, FakeResponse, service


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:1082", "http://[::1]:1082", ""])
def test_proxy_config_valid(proxy, monkeypatch):
    monkeypatch.setenv("A3_TAVILY_HTTP_PROXY", proxy)
    assert web.WebConfig.from_env().tavily_http_proxy == proxy


@pytest.mark.parametrize(
    "proxy",
    [
        "http://localhost:1082",
        "https://127.0.0.1:1082",
        "http://10.0.0.1:1082",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://user@127.0.0.1:1082",
        "http://127.0.0.1:1082/",
        "http://127.0.0.1:1082?x",
        "http://127.0.0.1:1082#x",
    ],
)
def test_proxy_config_rejects(proxy):
    with pytest.raises(ValueError):
        web.WebConfig(tavily_http_proxy=proxy)


def mock_vendor(svc, monkeypatch, data, status=200):
    monkeypatch.setattr(
        web,
        "resolve_public_url",
        lambda url: web.PublicTarget(
            url, "api.tavily.com", "93.184.216.34", 443, "https", "/extract"
        ),
    )
    calls = []

    def factory(target, timeout):
        c = FakeConnection(FakeResponse(json.dumps(data).encode(), status=status))
        calls.append((target, timeout, c))
        return c

    svc.search_connection_factory = factory
    return calls


def test_extract_active_path_no_target_dns(tmp_path, monkeypatch):
    svc = service(tmp_path)
    calls = mock_vendor(
        svc,
        monkeypatch,
        {
            "results": [{"url": "https://example.org/a", "raw_content": "abcdef", "title": "A"}],
            "failed_results": [],
        },
    )
    result = svc.execute(
        "fetch", web.FetchRequest(url="https://example.org/a#x", max_chars=3), "alice"
    )
    assert result["provider"] == "tavily" and result["untrusted"]
    assert result["sources"] == [
        {
            "url": "https://example.org/a",
            "text": "abc",
            "title": "A",
            "published_at": None,
            "truncated": True,
        }
    ]
    target, timeout, conn = calls[0]
    assert target.url == "https://api.tavily.com/extract" and 0 < timeout <= 20
    assert conn.requests[0][0] == ("POST", "/extract")
    assert json.loads(conn.requests[0][1]["body"]) == {
        "urls": ["https://example.org/a"],
        "extract_depth": "basic",
        "include_images": False,
        "include_favicon": False,
        "format": "markdown",
    }
    assert svc.status()["fetch_backend"] == "tavily_extract"


@pytest.mark.parametrize(
    "data",
    [
        {"results": [], "failed_results": [{"url": "https://example.org", "error": "secret"}]},
        {"results": [{"url": "https://wrong.org", "raw_content": "wrong"}]},
        {"results": [{"url": "https://example.org", "raw_content": "  "}]},
    ],
)
def test_extract_failure_not_success(tmp_path, monkeypatch, data):
    svc = service(tmp_path)
    mock_vendor(svc, monkeypatch, data)
    with pytest.raises(web.WebError, match="extraction_failed"):
        svc.execute("fetch", web.FetchRequest(url="https://example.org"), "alice")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://a.local",
        "http://a.internal",
        "http://a.test",
        "http://a.onion",
        "http://metadata.google.internal",
        "http://127.0.0.1",
        "http://8.8.8.8",
        "http://[::1]",
        "http://2130706433",
        "http://127.1",
        "https://x.example:8080",
        "https://user@a.org",
        "https://a.org/a.pdf",
        "https://a.org/model.safetensors",
    ],
)
def test_extract_rejects_without_vendor(tmp_path, monkeypatch, url):
    svc = service(tmp_path)
    calls = mock_vendor(svc, monkeypatch, {})
    with pytest.raises(web.WebError):
        svc.execute("fetch", web.FetchRequest(url=url), "alice")
    assert calls == []


def test_actual_connect_parser_and_tls_only_auth(monkeypatch):
    sent, contexts = [], []

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            assert target == ("127.0.0.1", 1082)

        def sendall(self, data):
            sent.append(data)

        def makefile(self, *args):
            return io.BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def do_handshake(self):
            pass

        def close(self):
            pass

        def shutdown(self, how):
            pass

    sock = Sock()
    monkeypatch.setattr(web.socket, "socket", lambda *args: sock)
    real_context = ssl.create_default_context

    def context():
        ctx = real_context()
        contexts.append((ctx.check_hostname, ctx.verify_mode))

        class Wrapper:
            def wrap_socket(self, raw, server_hostname, do_handshake_on_connect):
                assert server_hostname == "api.tavily.com" and not do_handshake_on_connect
                assert b"Authorization" not in b"".join(sent)
                return raw

        return Wrapper()

    monkeypatch.setattr(web.ssl, "create_default_context", context)
    conn = web.TavilyProxyConnection("http://127.0.0.1:1082", 1)
    try:
        conn.request("POST", "/extract", body=b"{}", headers={"Authorization": "Bearer secret"})
    finally:
        conn.finish()
    assert sent[0] == b"CONNECT api.tavily.com:443 HTTP/1.1\r\nHost: api.tavily.com:443\r\n\r\n"
    assert b"Bearer secret" in b"".join(sent[1:])
    assert contexts == [(True, ssl.CERT_REQUIRED)]


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_proxy_active_path_never_resolves_or_falls_back(tmp_path, monkeypatch, operation):
    svc = service(tmp_path, tavily_http_proxy="http://127.0.0.1:1082")
    monkeypatch.setattr(web, "resolve_public_url", lambda *a: pytest.fail("local vendor DNS"))
    svc.search_connection_factory = lambda *a: pytest.fail("direct fallback")
    calls = []

    def broken(proxy, timeout):
        calls.append((proxy, timeout))
        raise OSError("secret proxy failure")

    monkeypatch.setattr(web, "TavilyProxyConnection", broken)
    body = (
        web.SearchRequest(query="public")
        if operation == "search"
        else web.FetchRequest(url="https://example.org")
    )
    with pytest.raises(web.WebError, match="upstream_error"):
        svc.execute(operation, body, "alice")
    assert len(calls) == 1
    assert calls[0][0] == "http://127.0.0.1:1082"
    assert 0 < calls[0][1] <= (15 if operation == "search" else 20)
    assert svc.status()["transport_mode"] == "http_connect"
    assert "127.0.0.1" not in json.dumps(svc.status())


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "provider_error"),
        (401, "provider_auth_error"),
        (429, "provider_rate_limited"),
        (432, "provider_quota_exhausted"),
        (433, "provider_quota_exhausted"),
    ],
)
def test_extract_provider_failure_stable(tmp_path, monkeypatch, status, code):
    svc = service(tmp_path)
    calls = mock_vendor(svc, monkeypatch, {"secret": "secret-key"}, status)
    with pytest.raises(web.WebError, match=code):
        svc.execute("fetch", web.FetchRequest(url="https://example.org"), "alice")
    assert len(calls) == 1 and calls[0][2].closed
    assert "secret-key" not in (svc.root / "events.jsonl").read_text()


@pytest.mark.parametrize("phase", ["connect", "tls", "headers", "body"])
def test_proxy_absolute_deadline_all_phases(monkeypatch, phase):
    import threading

    stopped = threading.Event()
    sent = []

    def blocked():
        assert stopped.wait(2), "watchdog did not stop blocked I/O"
        raise OSError("aborted")

    class BlockedFile(io.BytesIO):
        def readline(self, limit=-1):
            if phase in ("connect", "headers"):
                return blocked()
            return super().readline(limit)

        def read1(self, count=-1):
            return blocked()

    class Sock:
        files = 0

        def settimeout(self, value):
            pass

        def connect(self, target):
            pass

        def sendall(self, data):
            sent.append(data)

        def makefile(self, *args):
            self.files += 1
            if self.files == 1:
                if phase == "connect":
                    return BlockedFile()
                return io.BytesIO(b"HTTP/1.1 200 OK\r\n\r\n")
            return BlockedFile(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")

        def do_handshake(self):
            if phase == "tls":
                blocked()

        def shutdown(self, how):
            stopped.set()

        def close(self):
            pass

    class Context:
        def wrap_socket(self, raw, **kw):
            return raw

    monkeypatch.setattr(web.socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    conn = web.TavilyProxyConnection("http://127.0.0.1:1082", 0.03)
    try:
        with pytest.raises(OSError, match="aborted"):
            conn.request("POST", "/extract", body=b"{}", headers={"Authorization": "Bearer secret"})
            response = conn.getresponse()
            assert conn.timer.is_alive()
            response.read1(100)
        if phase in ("connect", "tls"):
            assert b"Bearer" not in b"".join(sent)
    finally:
        conn.finish()


@pytest.mark.parametrize("failure", ["proxy", "certificate"])
def test_proxy_failure_never_sends_authorization(monkeypatch, failure):
    sent = []

    class Sock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            pass

        def sendall(self, data):
            sent.append(data)

        def makefile(self, *args):
            return io.BytesIO(
                b"HTTP/1.1 "
                + (b"407 Unauthorized" if failure == "proxy" else b"200 OK")
                + b"\r\n\r\n"
            )

        def close(self):
            pass

    class Context:
        def wrap_socket(self, *a, **kw):
            raise ssl.SSLCertVerificationError("bad cert")

    monkeypatch.setattr(web.socket, "socket", lambda *a: Sock())
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    conn = web.TavilyProxyConnection("http://127.0.0.1:1082", 1)
    try:
        with pytest.raises((web.WebError, ssl.SSLCertVerificationError)):
            conn.request("POST", "/search", headers={"Authorization": "Bearer secret"})
    finally:
        conn.finish()
    assert len(sent) == 1 and b"Authorization" not in sent[0]


def test_cache_shared_across_runs_refresh_and_counts(tmp_path):
    calls = []

    def search(body):
        calls.append(body.query)
        return []

    first = web.WebGateway(
        tmp_path / "run1", web.WebConfig(True, "key"), cache_root=tmp_path / "shared"
    )
    first.search = search
    a = first.execute("search", web.SearchRequest(query="unique public"), "alice")
    second = web.WebGateway(
        tmp_path / "run2", web.WebConfig(True, "key"), cache_root=tmp_path / "shared"
    )
    second.search = search
    b = second.execute("search", web.SearchRequest(query="unique public"), "bob")
    assert len(calls) == 1 and b["cached"] and not a["cached"]
    assert a["retrieved_at"] == b["retrieved_at"] and a["request_id"] != b["request_id"]
    assert b["cache_age_seconds"] >= 0 and b["served_at"] >= b["retrieved_at"]
    assert second.budget()["search"]["used"] == 1
    assert second.budget()["provider_requests"]["search"] == 0
    c = second.execute("search", web.SearchRequest(query="unique public", refresh=True), "bob")
    assert len(calls) == 2 and not c["cached"]
    assert second.budget()["provider_requests"]["search"] == 1
    assert second.budget()["search"]["used"] == 2
    for file in (tmp_path / "shared").rglob("*.json"):
        assert "unique public" not in file.read_text()


@pytest.mark.parametrize(
    "model,field",
    [(web.SearchRequest, {"query": "q"}), (web.FetchRequest, {"url": "https://example.org"})],
)
def test_refresh_strict_boolean(model, field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        model(**field, refresh="true")
    assert model(**field, refresh=True).refresh is True


@pytest.mark.parametrize("config", [{"cache_max_bytes": 0}, {"search_ttl_seconds": 0}])
def test_cache_zero_disables_requests(tmp_path, config):
    svc = service(tmp_path, **config)
    calls = []
    svc.search = lambda body: calls.append(1) or []
    for _ in range(2):
        assert not svc.execute("search", web.SearchRequest(query="q"), "alice")["cached"]
    assert len(calls) == 2


def test_refresh_failure_does_not_silently_serve_stale(tmp_path):
    svc = service(tmp_path)
    svc.search = lambda body: []
    svc.execute("search", web.SearchRequest(query="q"), "alice")

    def failed(body):
        raise web.WebError("provider_quota_exhausted", 429)

    svc.search = failed
    with pytest.raises(web.WebError, match="provider_quota_exhausted"):
        svc.execute("search", web.SearchRequest(query="q", refresh=True), "alice")
    assert svc.execute("search", web.SearchRequest(query="q"), "alice")["cached"]
    assert svc.budget()["provider_requests"]["search"] == 2


def test_cache_keys_options_and_success_only(tmp_path):
    svc = service(tmp_path)
    calls = []

    def failed(body):
        calls.append(1)
        raise web.WebError("provider_error", 502)

    svc.search = failed
    for _ in range(2):
        with pytest.raises(web.WebError):
            svc.execute("search", web.SearchRequest(query="q"), "alice")
    assert len(calls) == 2
    svc.search = lambda body: calls.append(1) or []
    svc.execute("search", web.SearchRequest(query="q"), "alice")
    svc.execute("search", web.SearchRequest(query="q", max_results=3), "alice")
    svc.execute("search", web.SearchRequest(query="q", domains=["example.org"]), "alice")
    assert len(calls) == 5


def test_cache_singleflight_across_services(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []

    def search(body):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return []

    services = [
        web.WebGateway(
            tmp_path / f"run{i}", web.WebConfig(True, "key"), cache_root=tmp_path / "shared"
        )
        for i in range(2)
    ]
    for svc in services:
        svc.search = search

    def run(svc):
        return svc.execute("search", web.SearchRequest(query="q"), "alice")

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(run, services[0])
        assert entered.wait(2)
        b = pool.submit(run, services[1])
        release.set()
        responses = [a.result(), b.result()]
    assert len(calls) == 1
    assert sorted(r["cached"] for r in responses) == [False, True]


def test_extract_active_content_limit(tmp_path, monkeypatch):
    svc = service(tmp_path)
    calls = mock_vendor(
        svc,
        monkeypatch,
        {"results": [{"url": "https://example.org", "raw_content": "x" * web.MAX_BYTES}]},
    )
    with pytest.raises(web.WebError, match="response_too_large"):
        svc.execute("fetch", web.FetchRequest(url="https://example.org"), "alice")
    assert calls[0][2].closed


@pytest.mark.parametrize("url", ["http://a.localdomain", "https://a.alt"])
def test_extract_additional_special_use_names(url):
    with pytest.raises(web.WebError, match="unsafe_url"):
        web.validate_extract_url(url)


def test_cache_unavailable_is_visible_and_live_success_not_false_hit(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("keep")
    svc = web.WebGateway(tmp_path / "run", web.WebConfig(True, "key"), cache_root=blocked)
    svc.search = lambda body: []
    result = svc.execute("search", web.SearchRequest(query="q"), "alice")
    assert not result["cached"]
    assert result["cache_warning"] == "cache_unavailable"
    assert svc.status()["cache"]["warning"] == "cache_unavailable"
    assert (
        json.loads((svc.root / "events.jsonl").read_text())["cache_warning"] == "cache_unavailable"
    )
    assert blocked.read_text() == "keep"


def test_cache_lock_busy_never_calls_provider(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from runtime.local.web_cache import CacheBusy

    svc = service(tmp_path)

    @contextmanager
    def busy(key):
        raise CacheBusy()
        yield

    monkeypatch.setattr(svc.cache, "lock", busy)
    svc.search = lambda body: pytest.fail("must not consume duplicate credits")
    with pytest.raises(web.WebError, match="cache_busy") as caught:
        svc.execute("search", web.SearchRequest(query="q"), "alice")
    assert caught.value.status == 429
    assert svc.budget()["provider_requests"]["search"] == 0
