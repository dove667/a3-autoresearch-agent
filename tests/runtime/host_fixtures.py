"""Start native test sessions through the Host-only startup boundary."""

import httpx
from fastapi import HTTPException


def host_create_session(client, *, headers, json):
    request = httpx.Request("POST", "http://host-fixture/start")
    try:
        result = client.app.state.start_local_harness(
            json["command"],
            json.get("files", {}),
            headers["Authorization"],
            read_only_exports=json.get("read_only_exports", []),
        )
    except HTTPException as error:
        return httpx.Response(error.status_code, json={"detail": error.detail}, request=request)
    return httpx.Response(200, json=result, request=request)
