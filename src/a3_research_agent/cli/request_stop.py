"""Ask a running research process to stop after its current turn."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Trajectory directory of the running research")
    parser.add_argument("--reason", default="operator request", help="Why the run should stop")
    args = parser.parse_args()
    conversation = args.run / "conversation"
    if not conversation.is_dir():
        raise SystemExit(f"No conversation directory under {args.run}")
    target = conversation / "stop-request.json"
    target.write_text(json.dumps({"reason": args.reason[:200]}, ensure_ascii=False) + "\n")
    print(f"Stop requested: {target}")
    print("The process stops after its current turn; wait for it to exit before starting again.")


if __name__ == "__main__":
    main()
