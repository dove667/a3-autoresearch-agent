"""Direct macOS upstream transport that bypasses a system TUN proxy."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_IP_CACHE: dict[str, tuple[float, str]] = {}


class DirectTransportError(RuntimeError):
    pass


@dataclass
class DirectHTTPError(RuntimeError):
    status_code: int
    body: str

    def __str__(self):
        return f"direct upstream HTTP {self.status_code}"


def _public_ipv4(values):
    for value in values:
        try:
            address = ipaddress.ip_address(value.strip())
        except ValueError:
            continue
        if address.version == 4:
            return str(address)
    return None


def resolve_direct(host: str, dns: str = "223.5.5.5") -> str:
    """Resolve through a public resolver, not the TUN's fake-IP DNS."""
    now = time.monotonic()
    cached = _IP_CACHE.get(host)
    if cached and now - cached[0] < 300:
        return cached[1]
    for command in (["dig", "+short", "@" + dns, host], ["nslookup", host, dns]):
        try:
            output = subprocess.check_output(command, text=True, timeout=10, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        address = _public_ipv4(output.splitlines())
        if address:
            _IP_CACHE[host] = (now, address)
            return address
    raise DirectTransportError("Direct DNS resolution failed; refusing to fall back to a proxy")


def interface_address(interface: str) -> str:
    try:
        return subprocess.check_output(
            ["ipconfig", "getifaddr", interface], text=True, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def post_json(url: str, headers: dict[str, str], body: dict, timeout: int) -> dict:
    """POST through the physical interface and a resolved public address.

    ``curl --interface`` plus an explicit ``--resolve`` bypasses a Shadowrocket
    style TUN route. The key is written to a temporary curl config so it never
    appears in the process argument list.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DirectTransportError("Direct upstream requires an HTTPS URL")
    interface = os.environ.get("A3_UPSTREAM_DIRECT_INTERFACE", "en0").strip()
    if not interface or not interface_address(interface):
        raise DirectTransportError(f"Direct upstream interface is unavailable: {interface}")
    address = resolve_direct(parsed.hostname, os.environ.get("A3_UPSTREAM_DIRECT_DNS", "223.5.5.5"))
    port = parsed.port or 443
    output_fd, output_name = tempfile.mkstemp(prefix=".direct-upstream-")
    os.close(output_fd)
    config_fd, config_name = tempfile.mkstemp(prefix=".direct-curl-", text=True)
    try:
        os.chmod(config_name, 0o600)
        lines = [
            f'url = "{url}"',
            'request = "POST"',
            f'interface = "{interface}"',
            'noproxy = "*"',
            f'resolve = "{parsed.hostname}:{port}:{address}"',
            f'max-time = "{int(timeout)}"',
            'connect-timeout = "10"',
            'silent',
            'show-error',
            f'output = "{output_name}"',
            'write-out = "%{http_code}"',
        ]
        for name, value in headers.items():
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'header = "{name}: {escaped}"')
        with os.fdopen(config_fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        command = ["curl", "-K", config_name, "--data-binary", "@-"]
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(body, ensure_ascii=False).encode(),
                capture_output=True,
                timeout=timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DirectTransportError("Direct upstream timed out") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace")[:300]
            raise DirectTransportError(f"Direct upstream transport failed: {detail}")
        try:
            status = int(completed.stdout.decode().strip() or "0")
        except ValueError as error:
            raise DirectTransportError("Direct upstream returned no HTTP status") from error
        raw = Path(output_name).read_text(encoding="utf-8")
        if status >= 400:
            raise DirectHTTPError(status, raw[:1000])
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise DirectTransportError("Direct upstream returned invalid JSON") from error
        if not isinstance(data, dict):
            raise DirectTransportError("Direct upstream returned a non-object response")
        return data
    finally:
        Path(config_name).unlink(missing_ok=True)
        Path(output_name).unlink(missing_ok=True)
