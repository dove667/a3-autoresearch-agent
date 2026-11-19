"""Authenticated dispatch and lookup for the main Agent's research tasks."""

import json
from typing import Annotated

from fastapi import Header, HTTPException, Query

from a3_research_agent.host.research.tasks import ResearchRequest


class ResearchRoutes:
    def research_owner(self, authorization):
        owner = self.owner(authorization)
        self.accepting_resources()
        if owner != self.main_owner:
            raise HTTPException(403, "Only the main Agent may delegate deep research")
        return owner

    def submit_research(
        self, body: ResearchRequest, authorization: Annotated[str | None, Header()] = None
    ):
        owner = self.research_owner(authorization)

        def model(request):
            result = self.forward_response(request, authorization, purpose="research")
            return result if isinstance(result, dict) else json.loads(result.body)

        return self.research_tasks.submit(owner, body, model)

    def research_status(
        self,
        task_id: str,
        wait_seconds: Annotated[int, Query(ge=0, le=25)] = 0,
        authorization: Annotated[str | None, Header()] = None,
    ):
        owner = self.research_owner(authorization)
        task = self.research_tasks.lookup(owner, task_id, wait_seconds)
        if task is None:
            raise HTTPException(404, "Deep research task not found")
        return task
