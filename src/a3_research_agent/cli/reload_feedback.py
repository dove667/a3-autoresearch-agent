"""Preview and confirm a human feedback file without interrupting the running Agent."""

import argparse
import json
from pathlib import Path

import httpx

from a3_research_agent.host.inputs.feedback import MAX_FEEDBACK_BYTES, HumanFeedback
from a3_research_agent.host.runs.state import RunStateStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Running experiment directory")
    parser.add_argument("--feedback-file", type=Path, required=True, help="UTF-8 human feedback")
    parser.add_argument("--confirm", help="Exact content and recipient digest from the preview")
    args = parser.parse_args()
    with args.feedback_file.open("rb") as stream:
        text = HumanFeedback.validate_content(stream.read(MAX_FEEDBACK_BYTES + 1))
    state = RunStateStore(args.run.resolve()).load()
    port = int(state["endpoint"].rsplit(":", 1)[1])
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer " + state["operator_token"]},
        timeout=60,
    ) as client:
        response = client.post("/control/feedback/preview", json={"text": text})
        response.raise_for_status()
        proposal = response.json()
        print("警告：" + proposal["warning"])
        print(
            f"接收身份：{proposal['owner']}；{proposal['bytes']} bytes；SHA256={proposal['sha256']}"
        )
        print(text)
        if args.confirm is None:
            print("仅预览，尚未注入。核对后增加 --confirm " + proposal["digest"])
            return
        if args.confirm != proposal["digest"]:
            raise SystemExit("确认内容或主身份已过期；请重新预览后使用当前摘要")
        response = client.post("/control/feedback", json={"text": text, "confirm": args.confirm})
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
