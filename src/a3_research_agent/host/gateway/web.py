"""Web Gateway behavior on the shared runtime instance."""

from typing import Annotated

from fastapi import Header, HTTPException

from a3_research_agent.host.telemetry.diagnostics import CURRENT_REQUEST
from a3_research_agent.host.web.gateway import FetchRequest, SearchRequest, WebError


class WebRoutes:
    def web_call(self, operation, body, authorization):
        actor = self.owner(authorization)
        self.accepting_resources()
        try:
            return self.web_service.execute(operation, body, actor)
        except WebError as error:
            request = CURRENT_REQUEST.get() or {}
            raise HTTPException(
                error.status,
                {
                    "code": error.code,
                    "request_id": request.get("request_id"),
                    "message": error.code.replace("_", " "),
                },
            ) from None

    def web_status(self, authorization: Annotated[str | None, Header()] = None):
        self.owner(authorization)
        self.accepting_resources()
        try:
            return self.web_service.status()
        except WebError as error:
            raise HTTPException(error.status, {"code": error.code}) from None

    def web_search(
        self, body: SearchRequest, authorization: Annotated[str | None, Header()] = None
    ):
        return self.web_call("search", body, authorization)

    def web_fetch(self, body: FetchRequest, authorization: Annotated[str | None, Header()] = None):
        return self.web_call("fetch", body, authorization)
