"""Fast, model-free checks that the real benchmark can actually start."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from a3_research_agent.harness import tool_catalog
from a3_research_agent.harness.plan import store as plan_store
from a3_research_agent.host.models.direct import interface_address, resolve_direct
from a3_research_agent.project import checkout_root

REPO = checkout_root() or Path(__file__).resolve().parents[3]
REQUIRED_TOOLS = {
    "list",
    "read",
    "write",
    "search_files",
    "plan",
    "memory",
    "skills",
    "recall",
    "job_submit",
    "job_wait",
    "job_status",
    "research_status",
}


def check() -> dict:
    prompts = {
        name: (REPO / "prompt" / name).is_file()
        for name in ("SOUL.md", "execution.md", "workspace.md")
    }
    names = {tool["name"] for tool in tool_catalog.TOOLS}
    plan_path = REPO / "plan/current.json"
    plan_revision = None
    plan_error = None
    try:
        plan_revision = plan_store.load(plan_path)["revision"]
    except (OSError, ValueError) as error:
        plan_error = type(error).__name__
    interface = os.environ.get("A3_UPSTREAM_DIRECT_INTERFACE", "")
    direct = {
        "interface": interface,
        "address": interface_address(interface) if interface else "",
        "model": os.environ.get("A3_UPSTREAM_RESPONSES_MODEL", ""),
    }
    if interface and direct["address"]:
        try:
            direct["resolved_ip"] = resolve_direct(
                os.environ["A3_UPSTREAM_RESPONSES_BASE_URL"].split("//", 1)[-1].split("/", 1)[0]
            )
        except Exception as error:  # noqa: BLE001 - health report, not a crash
            direct["error"] = type(error).__name__
    knowledge = {
        "url": bool(os.environ.get("A3_KNOWLEDGE_SERVICE_URL")),
        "token": bool(os.environ.get("A3_KNOWLEDGE_SERVICE_TOKEN")),
        "memory_root": (REPO / "memory").is_dir(),
        "skills_root": (REPO / "skills").is_dir(),
    }
    proxy_names = [
        name
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        if os.environ.get(name)
    ]
    missing = sorted(REQUIRED_TOOLS - names)
    healthy = (
        all(prompts.values())
        and not missing
        and plan_error is None
        and bool(direct["address"])
        and bool(direct.get("resolved_ip"))
        and knowledge["memory_root"]
        and knowledge["skills_root"]
    )
    return {
        "healthy": healthy,
        "prompts": prompts,
        "tool_count": len(names),
        "missing_tools": missing,
        "plan_revision": plan_revision,
        "plan_error": plan_error,
        "direct": direct,
        "knowledge": knowledge,
        "proxy_environment": proxy_names,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["healthy"] else 1)


if __name__ == "__main__":
    main()
