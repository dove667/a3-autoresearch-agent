"""Host-only, opt-in research access. Web content is always untrusted data."""

import concurrent.futures
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from runtime.local.gateway_diagnostics import CURRENT_REQUEST

MAX_BYTES = 2 * 1024 * 1024
DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="web-dns")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=5, ge=1, le=10, strict=True)
    domains: list[str] | None = Field(default=None, max_length=20)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("domains")
    @classmethod
    def valid_domains(cls, values):
        if values is not None and any(
            not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}",
                v,
            )
            for v in values
        ):
            raise ValueError("domains must be public DNS names without paths")
        return values


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)
    max_chars: int = Field(default=16000, ge=1, le=16000, strict=True)


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        enabled = os.environ.get("A3_WEB_ENABLED", "0")
        if enabled not in ("0", "1"):
            raise ValueError("A3_WEB_ENABLED must be 0 or 1")
        # Removed legacy A3_WEB_SEARCH_LIMIT / A3_WEB_FETCH_LIMIT are ignored.
        return cls(enabled == "1", os.environ.get("TAVILY_API_KEY", "").strip())


class WebError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class PublicTarget:
    url: str
    host: str
    ip: str
    port: int
    scheme: str
    path: str


def resolve_public_url(url, resolver=socket.getaddrinfo):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(c) < 33 or ord(c) == 127 for c in url)
            or "\\" in url
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in (80, 443) or host.rstrip(".").lower() == "localhost":
            raise ValueError
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = (
            [str(literal)]
            if literal
            else [r[4][0] for r in resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)]
        )
        if not addresses or any(
            not ipaddress.ip_address(ip).is_global
            or ipaddress.ip_address(ip).is_multicast
            or any(
                ipaddress.ip_address(ip) in network
                for network in (
                    ipaddress.ip_network("64:ff9b::/96"),
                    ipaddress.ip_network("64:ff9b:1::/48"),
                    ipaddress.ip_network("2002::/16"),
                    ipaddress.ip_network("2001::/32"),
                )
            )
            or getattr(ipaddress.ip_address(ip), "ipv4_mapped", None) is not None
            for ip in addresses
        ):
            raise ValueError
        return PublicTarget(
            parsed._replace(fragment="").geturl(),
            host,
            addresses[0],
            port,
            parsed.scheme,
            (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
        )
    except (ValueError, UnicodeError, OSError):
        raise WebError("unsafe_url") from None


class PinnedConnection(http.client.HTTPConnection):
    """Never re-resolve the checked name: numeric socket connect + original TLS identity."""

    def __init__(self, target, timeout):
        super().__init__(target.host, target.port, timeout=timeout)
        self.target = target
        self.deadline = time.monotonic() + timeout
        self.network_socket = None
        self.timer = threading.Timer(timeout, self.abort)
        self.timer.daemon = True
        self.timer.start()

    def abort(self):
        sock = self.network_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self):
        # HTTPConnection calls close() immediately after Connection: close headers.
        # Preserve the socket and deadline while HTTPResponse still streams its body.
        self.sock = None
        super().close()

    def finish(self):
        self.timer.cancel()
        super().close()
        if self.network_socket is not None:
            self.network_socket.close()

    def connect(self):
        target = self.target
        sock = socket.socket(
            socket.AF_INET6 if ":" in target.ip else socket.AF_INET, socket.SOCK_STREAM
        )
        try:
            self.network_socket = sock
            sock.settimeout(remaining(self.deadline))
            sock.connect((target.ip, target.port))
            if target.scheme == "https":
                sock.settimeout(remaining(self.deadline))
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=target.host, do_handshake_on_connect=False
                )
                self.network_socket = sock
                sock.settimeout(remaining(self.deadline))
                sock.do_handshake()
            remaining(self.deadline)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.hidden = [], [], 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title_parts.append(data)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise WebError("timeout", 504)
    return value


def fetch_public(
    url, max_chars, *, resolver=socket.getaddrinfo, connection_factory=PinnedConnection
):
    deadline = time.monotonic() + 20
    for hop in range(4):
        future = DNS_POOL.submit(resolve_public_url, url, resolver)
        try:
            target = future.result(timeout=remaining(deadline))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise WebError("timeout", 504) from None
        connection = connection_factory(target, remaining(deadline))
        try:
            host = "[" + target.host + "]" if ":" in target.host else target.host
            default = 443 if target.scheme == "https" else 80
            if target.port != default:
                host += ":" + str(target.port)
            connection.request(
                "GET",
                target.path,
                headers={
                    "Host": host,
                    "Accept": "text/html,text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": "A3-Research-Gateway/1",
                },
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if hop == 3 or not location:
                    raise WebError("redirect_limit", 502)
                url = urljoin(target.url, location)
                continue
            if response.status != 200:
                raise WebError("upstream_error", 502)
            content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            if content_type not in ("text/html", "text/plain"):
                raise WebError("unsupported_content", 422)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise WebError("unsupported_encoding", 422)
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                raise WebError("response_too_large", 413)
            chunks, size = [], 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(remaining(deadline))
                data = response.read1(min(65536, MAX_BYTES + 1 - size))
                remaining(deadline)
                if not data:
                    break
                size += len(data)
                if size > MAX_BYTES:
                    raise WebError("response_too_large", 413)
                chunks.append(data)
            text = b"".join(chunks).decode("utf-8", errors="replace")
            title = ""
            if content_type == "text/html":
                parser = TextHTML()
                parser.feed(text)
                text, title = "".join(parser.parts), "".join(parser.title_parts)
            text = re.sub(r"[ \t]+", " ", text).strip()
            return {
                "title": title[:1000],
                "url": target.url,
                "text": text[:max_chars],
                "published_at": None,
                "truncated": len(text) > max_chars,
            }
        except Exception:
            remaining(deadline)
            raise
        finally:
            connection.finish()
    raise WebError("redirect_limit", 502)


class WebGateway:
    def __init__(self, root: Path, config: WebConfig):
        self.root, self.config = root, config
        self.parallel = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.search_connection_factory = PinnedConnection
        self.fetcher = fetch_public

    def budget(self, reserve=None):
        """Persistent attempt accounting; legacy name/file retained for compatibility."""
        try:
            return self._account_usage(reserve)
        except OSError:
            raise WebError("budget_unavailable", 503) from None

    def _account_usage(self, reserve=None):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock, (self.root / "budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / "budget.json"
            if path.exists():
                try:
                    state = json.loads(path.read_text())
                    for kind in ("search", "fetch"):
                        used, limit = state[kind]["used"], state[kind]["limit"]
                        if type(used) is not int or used < 0:
                            raise ValueError
                        if limit is not None and (type(limit) is not int or limit < 0):
                            raise ValueError
                except (ValueError, KeyError, TypeError, AssertionError):
                    raise WebError("budget_unavailable", 503) from None
            else:
                state = {kind: {"limit": None, "used": 0} for kind in ("search", "fetch")}
            # Migrate finite legacy limits under the same cross-process lock, never
            # reset used counts or touch historical audit events.
            migrated = any(state[kind]["limit"] is not None for kind in ("search", "fetch"))
            for kind in ("search", "fetch"):
                state[kind]["limit"] = None
            if reserve:
                state[reserve]["used"] += 1
            if reserve or migrated or not path.exists():
                temporary = path.with_suffix(".tmp")
                with temporary.open("w") as stream:
                    json.dump(state, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return state

    def status(self):
        return {
            "enabled": self.config.enabled,
            "configured": bool(self.config.api_key),
            "provider": "tavily",
            "capabilities": {
                "search": "POST /web/search",
                "fetch": "POST /web/fetch",
                "status": "GET /web/status",
            },
            "budget": self.budget(),
            "no_run_cap": True,
            "cached": False,
            "max_parallel": 2,
            "search_max_results": 10,
            "fetch_max_chars": 16000,
            "failed_requests_may_consume_budget": True,
        }

    def search(self, body):
        payload = {
            "query": body.query,
            "max_results": body.max_results,
            "topic": "general",
            "search_depth": "basic",
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
        }
        if body.domains is not None:
            payload["include_domains"] = body.domains
        deadline = time.monotonic() + 15
        future = DNS_POOL.submit(resolve_public_url, "https://api.tavily.com/search")
        try:
            target = future.result(timeout=remaining(deadline))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise WebError("timeout", 504) from None
        connection = self.search_connection_factory(target, remaining(deadline))
        try:
            connection.request(
                "POST",
                "/search",
                body=json.dumps(payload).encode(),
                headers={
                    "Host": "api.tavily.com",
                    "Authorization": "Bearer " + self.config.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                code, status = {
                    401: ("provider_auth_error", 502),
                    429: ("provider_rate_limited", 429),
                    432: ("provider_quota_exhausted", 429),
                    433: ("provider_quota_exhausted", 429),
                }.get(response.status, ("provider_error", 502))
                # Never return provider bodies/headers: they may contain secrets.
                raise WebError(code, status)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise WebError("unsupported_encoding", 422)
            chunks, size = [], 0
            while True:
                remaining(deadline)
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                remaining(deadline)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise WebError("response_too_large", 413)
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except Exception:
            remaining(deadline)
            raise
        finally:
            connection.finish()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise WebError("provider_error", 502)
        sources = []
        for entry in data["results"][: body.max_results]:
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                continue
            if urlsplit(entry["url"]).scheme not in ("http", "https"):
                continue
            content = entry.get("content") if isinstance(entry.get("content"), str) else ""
            published = entry.get("published_date")
            try:
                published = (
                    datetime.fromisoformat(published).isoformat()
                    if isinstance(published, str)
                    else None
                )
            except ValueError:
                published = None
            sources.append(
                {
                    "title": str(entry.get("title") or "")[:1000],
                    "url": entry["url"][:4096],
                    "text": content[:16000],
                    "published_at": published,
                    "truncated": len(content) > 16000,
                }
            )
        return sources

    def execute(self, operation, body, owner):
        request = CURRENT_REQUEST.get()
        request_id = request["request_id"] if request else uuid.uuid4().hex
        started, code, reserved = time.monotonic(), "ok", False
        fingerprint = hashlib.sha256(
            (body.query if operation == "search" else body.url).encode()
        ).hexdigest()
        acquired = False
        budget_state = None
        try:
            if not self.config.enabled:
                raise WebError("disabled", 503)
            if not self.config.api_key:
                raise WebError("unconfigured", 503)
            acquired = self.parallel.acquire(blocking=False)
            if not acquired:
                raise WebError("busy", 429)
            budget_state = self.budget(operation)
            reserved = True
            sources = (
                self.search(body)
                if operation == "search"
                else [self.fetcher(body.url, body.max_chars)]
            )
            return {
                "request_id": request_id,
                "provider": "tavily" if operation == "search" else "public_http",
                "retrieved_at": datetime.now(UTC).isoformat(),
                "sources": sources,
                "truncated": any(s["truncated"] for s in sources),
                "cached": False,
                "untrusted": True,
            }
        except TimeoutError:
            code = "timeout"
            raise WebError(code, 504) from None
        except WebError as error:
            code = error.code
            raise
        except Exception:  # noqa: BLE001 — redact query, credentials, and provider errors.
            code = "upstream_error"
            raise WebError(code, 502) from None
        finally:
            if acquired:
                self.parallel.release()
            event = {
                "request_id": request_id,
                "owner": owner,
                "operation": operation,
                "input_sha256": fingerprint,
                "status": code,
                "time": datetime.now(UTC).isoformat(),
                "wall_seconds": time.monotonic() - started,
                "budget_reserved": reserved,
                "budget": budget_state,
            }
            with self.lock:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with (self.root / "events.jsonl").open("a") as stream:
                    stream.write(json.dumps(event) + "\n")
