"""Built-in schemas for the Host's configured research web gateway."""

from .tool_contracts import ToolInputError, arguments, boolean, definition, fail, integer, text

SEARCH_FIELDS = {
    "query": {"type": "string", "minLength": 1, "maxLength": 1000},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    "domains": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    "refresh": {"type": "boolean"},
}
FETCH_FIELDS = {
    "url": {"type": "string", "minLength": 1, "maxLength": 4096},
    "max_chars": {"type": "integer", "minimum": 1, "maximum": 16000},
    "refresh": {"type": "boolean"},
}
TOOLS = [
    definition(
        "web_search",
        "Search the configured research web service for a specific question. "
        "Use domains for official sources. Host handles credentials, caching and public-URL checks.",
        SEARCH_FIELDS,
        ["query"],
    ),
    definition(
        "web_fetch",
        "Fetch bounded readable text from a public URL through the Host web service. "
        "Use after search to inspect the actual source; default max_chars=6000.",
        FETCH_FIELDS,
        ["url"],
    ),
    definition(
        "web_status", "Check whether research web search/fetch is configured and available.", {}
    ),
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def dispatch(name, args, request):
    try:
        if name == "web_status":
            arguments(args, {})
            return request("GET", "/web/status")
        fields = (
            SEARCH_FIELDS if name == "web_search" else FETCH_FIELDS if name == "web_fetch" else None
        )
        if fields is None:
            fail("unknown_web_tool", "Use web_search, web_fetch or web_status.")
        arguments(args, fields)
        body = dict(args)
        if name == "web_search":
            text(body.get("query"), "query", 1000)
            if "max_results" in body:
                integer(body["max_results"], "max_results", 1, 10)
            domains = body.get("domains", [])
            if (
                not isinstance(domains, list)
                or len(domains) > 20
                or any(not isinstance(domain, str) or len(domain) > 253 for domain in domains)
            ):
                fail("invalid_domains", "Use at most 20 public DNS names without URL paths.")
        else:
            text(body.get("url"), "url", 4096)
            body["max_chars"] = integer(body.get("max_chars", 6000), "max_chars", 1, 16000)
        if "refresh" in body:
            boolean(body["refresh"], "refresh")
        return request("POST", "/web/" + ("search" if name == "web_search" else "fetch"), body)
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
