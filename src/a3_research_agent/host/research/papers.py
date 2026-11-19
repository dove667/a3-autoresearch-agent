"""Read-only alphaXiv MCP transport using an operator-owned credential."""

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ENDPOINT = "https://api.alphaxiv.org/mcp/v1"
PAPER_TOOLS = {"discover_papers", "answer_pdf_queries"}


class PaperServiceError(RuntimeError):
    """A failed paper lookup with no provider payload attached."""


def cc_switch_token(path):
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            "SELECT server_config FROM mcp_servers WHERE id = ?", ("alphaxiv",)
        ).fetchone()
    if row:
        config = json.loads(row[0])
        if config.get("url") == ENDPOINT:
            for name, value in config.get("headers", {}).items():
                if name.lower() == "authorization" and value.startswith("Bearer "):
                    return value.removeprefix("Bearer ").strip()
    return ""


@dataclass(frozen=True)
class AlphaXiv:
    token: str = field(repr=False)

    @classmethod
    def from_env(cls):
        token = os.environ.get("A3_ALPHAXIV_TOKEN", "").strip()
        path = os.environ.get("A3_ALPHAXIV_CC_SWITCH_DB", "").strip()
        if not token and path:
            try:
                token = cc_switch_token(path)
            except (OSError, ValueError, sqlite3.Error):
                return None
        return cls(token) if token else None

    def call(self, name, arguments):
        if name not in PAPER_TOOLS:
            raise PaperServiceError("unsupported_paper_tool")
        try:
            return asyncio.run(self._call(name, arguments))
        except Exception:  # noqa: BLE001 - never expose MCP connection or credential details
            raise PaperServiceError("paper_provider_unavailable") from None

    async def _call(self, name, arguments):
        async with (
            asyncio.timeout(90),
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self.token},
                timeout=httpx2.Timeout(75, connect=10),
                trust_env=False,
            ) as client,
            streamable_http_client(ENDPOINT, http_client=client) as (read, write),
            ClientSession(read, write, read_timeout_seconds=75) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if result.is_error:
                raise PaperServiceError("paper_tool_failed")
            text = "\n".join(block.text for block in result.content if block.type == "text")
            if not text.strip():
                raise PaperServiceError("paper_content_empty")
            return text
