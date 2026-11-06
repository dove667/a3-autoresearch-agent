"""Seed lifecycle fixtures through Host internals, never a research API permission."""

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from a3_research_agent.host.gateway.schemas import SessionRequest


def host_create_session(client, *, headers, json):
    """Create historical session fixtures for version, recovery and cleanup tests."""
    gateway = next(
        route.endpoint.__self__
        for route in client.app.routes
        if getattr(route, "path", None) == "/sessions" and "POST" in route.methods
    )
    request = httpx.Request("POST", "http://host-fixture/internal-session")
    try:
        result = gateway._create_session(SessionRequest(**json), headers["Authorization"])
    except HTTPException as error:
        return httpx.Response(error.status_code, json={"detail": error.detail}, request=request)
    except ValidationError:
        return httpx.Response(422, json={"detail": "Invalid Host session fixture"}, request=request)
    return httpx.Response(200, json=result, request=request)
