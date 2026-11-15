"""Apply a frozen published release offline, retaining the current research workspace."""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from a3_research_agent.host.published_harness import project_for_run
from a3_research_agent.host.releases import apply_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Existing suspended published run")
    parser.add_argument("release", type=Path, help="Frozen published release directory")
    parser.add_argument("--digest", required=True, help="Exact published release digest")
    args = parser.parse_args()
    run = args.run.resolve()
    try:
        root = project_for_run(run)
        load_dotenv(os.environ.get("A3_RSI_ENV_FILE", root / ".env"), override=True)
        result = apply_release(run, args.release, args.digest)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
