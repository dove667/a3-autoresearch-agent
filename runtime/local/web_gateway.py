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
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from runtime.local.gateway_diagnostics import CURRENT_REQUEST
from runtime.local.web_cache import CacheBusy, WebCache

MAX_BYTES = 2 * 1024 * 1024
DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="web-dns")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: StrictBool = False
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
    refresh: StrictBool = False
    url: str = Field(min_length=1, max_length=4096)
    max_chars: int = Field(default=16000, ge=1, le=16000, strict=True)


def local_http_proxy(value):
    """Host-only explicit numeric loopback HTTP proxy; never consult proxy env vars."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or "?" in value
            or "#" in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or not parsed.port
            or not ipaddress.ip_address(parsed.hostname).is_loopback
        ):
            raise ValueError
        return parsed.hostname, parsed.port
    except (ValueError, TypeError):
        raise ValueError(
            "A3_TAVILY_HTTP_PROXY must be an explicit numeric loopback HTTP proxy"
        ) from None


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)

    tavily_http_proxy: str = field(default="", repr=False)

    cache_max_bytes: int = 500_000_000
    search_ttl_seconds: int = 86400
    fetch_ttl_seconds: int = 604800

    def __post_init__(self):
        for value in (self.cache_max_bytes, self.search_ttl_seconds, self.fetch_ttl_seconds):
            if type(value) is not int or value < 0:
                raise ValueError("Web cache settings must be nonnegative integers")
        if self.tavily_http_proxy:
            local_http_proxy(self.tavily_http_proxy)

    @classmethod
    def from_env(cls):
        enabled = os.environ.get("A3_WEB_ENABLED", "0")
        if enabled not in ("0", "1"):
            raise ValueError("A3_WEB_ENABLED must be 0 or 1")
        # Removed legacy A3_WEB_SEARCH_LIMIT / A3_WEB_FETCH_LIMIT are ignored.
        return cls(
            enabled == "1",
            os.environ.get("TAVILY_API_KEY", "").strip(),
            os.environ.get("A3_TAVILY_HTTP_PROXY", ""),
            int(os.environ.get("A3_WEB_CACHE_MAX_BYTES", "500000000")),
            int(os.environ.get("A3_WEB_SEARCH_TTL_SECONDS", "86400")),
            int(os.environ.get("A3_WEB_FETCH_TTL_SECONDS", "604800")),
        )


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


class TavilyProxyConnection(PinnedConnection):
    """Only tunnel the constant vendor identity; authenticate after verified TLS."""

    def __init__(self, proxy, timeout):
        self.proxy_host, self.proxy_port = local_http_proxy(proxy)
        super().__init__(
            PublicTarget(
                "https://api.tavily.com", "api.tavily.com", self.proxy_host, 443, "https", "/"
            ),
            timeout,
        )

    def connect(self):
        sock = socket.socket(
            socket.AF_INET6 if ":" in self.proxy_host else socket.AF_INET, socket.SOCK_STREAM
        )
        try:
            self.network_socket = sock
            sock.settimeout(remaining(self.deadline))
            sock.connect((self.proxy_host, self.proxy_port))
            sock.sendall(b"CONNECT api.tavily.com:443 HTTP/1.1\r\nHost: api.tavily.com:443\r\n\r\n")
            # The standard parser bounds status/header lines and header count.
            response = http.client.HTTPResponse(sock, method="CONNECT")
            try:
                response.begin()
                if response.status != 200:
                    raise WebError("proxy_error", 502)
            finally:
                response.close()
            sock.settimeout(remaining(self.deadline))
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname="api.tavily.com", do_handshake_on_connect=False
            )
            self.network_socket = sock
            sock.settimeout(remaining(self.deadline))
            sock.do_handshake()
            remaining(self.deadline)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


def validate_extract_url(url):
    """Lexical public-DNS-name policy, not DNS verification. Tavily fetches the URL."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or (parsed.port is not None and parsed.port not in (80, 443))
        ):
            raise ValueError
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host
        ):
            raise ValueError
        if host.split(".")[-1] in {
            "localhost",
            "local",
            "localdomain",
            "alt",
            "internal",
            "test",
            "invalid",
            "example",
            "onion",
            "home",
            "lan",
            "corp",
            "intranet",
            "arpa",
        }:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        path = unquote(parsed.path).lower()
        if re.search(
            r"\.(pdf|zip|gz|bz2|xz|tar|7z|exe|dll|bin|pt|pth|ckpt|safetensors|gguf|onnx|h5|hdf5|npy|npz|pkl|pickle|png|jpe?g|gif|webp|mp[34]|wav|woff2?)(?:/|$)",
            path,
        ):
            raise WebError("unsupported_content")
        return parsed._replace(fragment="").geturl()
    except (ValueError, UnicodeError):
        raise WebError("unsafe_url") from None


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
    # Legacy helper only: WebGateway never invokes local target fetching.
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
    def __init__(self, root: Path, config: WebConfig, *, cache_root: Path | None = None):
        self.root, self.config = root, config
        self.cache = WebCache(cache_root or root / "cache", max_bytes=config.cache_max_bytes)
        self.parallel = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.search_connection_factory = PinnedConnection
        self.fetcher = self.extract

    def budget(self, reserve=None, *, provider=None):
        """Persistent attempt accounting; legacy name/file retained for compatibility."""
        try:
            return self._account_usage(reserve, provider=provider)
        except OSError:
            raise WebError("budget_unavailable", 503) from None

    def _account_usage(self, reserve=None, *, provider=None):
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
            provider_counts = state.setdefault("provider_requests", {"search": 0, "fetch": 0})
            if not isinstance(provider_counts, dict) or any(
                type(provider_counts.get(kind)) is not int or provider_counts[kind] < 0
                for kind in ("search", "fetch")
            ):
                raise WebError("budget_unavailable", 503)
            if provider:
                provider_counts[provider] += 1
            # Migrate finite legacy limits under the same cross-process lock, never
            # reset used counts or touch historical audit events.
            migrated = any(state[kind]["limit"] is not None for kind in ("search", "fetch"))
            for kind in ("search", "fetch"):
                state[kind]["limit"] = None
            if reserve:
                state[reserve]["used"] += 1
            if reserve or provider or migrated or not path.exists():
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
            "proxy_configured": bool(self.config.tavily_http_proxy),
            "transport_mode": "http_connect" if self.config.tavily_http_proxy else "direct",
            "fetch_backend": "tavily_extract",
            "capabilities": {
                "search": "POST /web/search",
                "fetch": "POST /web/fetch",
                "status": "GET /web/status",
            },
            "budget": self.budget(),
            "no_run_cap": True,
            "cached": False,
            "cache": {
                "enabled": self.config.cache_max_bytes > 0,
                "max_bytes": self.config.cache_max_bytes,
                "search_ttl_seconds": self.config.search_ttl_seconds,
                "fetch_ttl_seconds": self.config.fetch_ttl_seconds,
                "warning": self.cache.warning,
            },
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
        data = self._vendor_post("/search", payload, 15)
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

    def _vendor_post(self, path, payload, timeout):
        if path not in ("/search", "/extract"):
            raise WebError("provider_error", 502)
        deadline = time.monotonic() + timeout
        if self.config.tavily_http_proxy:
            connection = TavilyProxyConnection(self.config.tavily_http_proxy, remaining(deadline))
        else:
            future = DNS_POOL.submit(resolve_public_url, "https://api.tavily.com" + path)
            try:
                target = future.result(timeout=remaining(deadline))
            except concurrent.futures.TimeoutError:
                future.cancel()
                raise WebError("timeout", 504) from None
            connection = self.search_connection_factory(target, remaining(deadline))
        try:
            connection.request(
                "POST",
                path,
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
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                raise WebError("response_too_large", 413)
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
        return data

    def extract(self, url, max_chars):
        url = validate_extract_url(url)
        data = self._vendor_post(
            "/extract",
            {
                "urls": [url],
                "extract_depth": "basic",
                "include_images": False,
                "include_favicon": False,
                "format": "markdown",
            },
            20,
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise WebError("provider_error", 502)
        failures = data.get("failed_results", [])
        if not isinstance(failures, list):
            raise WebError("provider_error", 502)
        if any(isinstance(item, dict) and item.get("url") == url for item in failures):
            raise WebError("extraction_failed", 502)
        for entry in data["results"]:
            if not isinstance(entry, dict) or entry.get("url") != url:
                continue
            content = entry.get("raw_content")
            if not isinstance(content, str) or not content.strip():
                continue
            return {
                "url": url,
                "title": str(entry.get("title") or "")[:1000],
                "text": content[:max_chars],
                "published_at": None,
                "truncated": len(content) > max_chars,
            }
        raise WebError("extraction_failed", 502)

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
            # Validate fetch inputs even for hits; never cache an unsafe URL request.
            if operation == "fetch":
                validate_extract_url(body.url)
            material = {
                "version": "tavily-basic-v1",
                "operation": operation,
                "options": body.model_dump(exclude={"refresh"}),
            }
            key = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
            ttl = (
                self.config.search_ttl_seconds
                if operation == "search"
                else self.config.fetch_ttl_seconds
            )
            with self.cache.lock(key):
                payload = None if body.refresh else self.cache.get(key, ttl)
                cached = payload is not None
                if payload is None:
                    # Conservative miss attempts, including provider/network failures.
                    budget_state = self.budget(provider=operation)
                    sources = (
                        self.search(body)
                        if operation == "search"
                        else [self.fetcher(body.url, body.max_chars)]
                    )
                    payload = {
                        "provider": "tavily",
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "sources": sources,
                        "truncated": any(s["truncated"] for s in sources),
                        "untrusted": True,
                    }
                    if ttl > 0:
                        self.cache.put(key, payload)
                served = datetime.now(UTC)
                return {
                    **payload,
                    "request_id": request_id,
                    "cached": cached,
                    "served_at": served.isoformat(),
                    "cache_warning": self.cache.warning,
                    "cache_age_seconds": max(
                        0.0,
                        (served - datetime.fromisoformat(payload["retrieved_at"])).total_seconds(),
                    )
                    if cached
                    else 0.0,
                }
        except CacheBusy:
            code = "cache_busy"
            raise WebError(code, 429) from None
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
                "cache_warning": self.cache.warning,
                "budget": budget_state,
            }
            with self.lock:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with (self.root / "events.jsonl").open("a") as stream:
                    stream.write(json.dumps(event) + "\n")
