"""Preview and explicitly confirm live Agent input updates as the Host operator."""

import argparse
import json
from pathlib import Path

import httpx

from a3_research_agent.host.runs.state import RunStateStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Running experiment directory")
    parser.add_argument(
        "--agent-inputs-dir",
        type=Path,
        help="Complete replacement for a custom run; same file names",
    )
    parser.add_argument("--confirm", help="Exact digest printed by the preview")
    args = parser.parse_args()
    state = RunStateStore(args.run.resolve()).load()
    port = int(state["endpoint"].rsplit(":", 1)[1])
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer " + state["operator_token"]},
        timeout=60,
    ) as client:
        selection = (
            {"source_dir": str(args.agent_inputs_dir.resolve())} if args.agent_inputs_dir else {}
        )
        response = client.get("/control/inputs", params=selection)
        response.raise_for_status()
        preview = response.json()
        print("警告：" + preview["warning"])
        for name, proposed in preview["proposed"]["files"].items():
            old = preview["current"].get("files", {}).get(name, {})
            print(
                f"{name}: {old.get('version', 'unversioned')} -> {proposed['version']} "
                f"sha256={proposed['sha256']}"
            )
        digest = preview["proposed"]["digest"]
        if args.confirm is None:
            print("仅预览，尚未更新。核对后增加 --confirm " + digest)
            return
        if args.confirm != digest:
            raise SystemExit("确认内容已过期；请重新预览后使用当前摘要")
        response = client.post("/control/inputs", json={"confirm": args.confirm, **selection})
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
