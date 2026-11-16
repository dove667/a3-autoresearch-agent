"""CLI: python -m a3_research_agent.benchmark [--model NAME] [--only id,id] [--out DIR]."""

import argparse

from a3_research_agent.benchmark.runner import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override the configured upstream model")
    parser.add_argument("--only", help="Comma-separated task ids")
    parser.add_argument("--out", help="Output directory under runs/checks")
    args = parser.parse_args()
    run(
        model=args.model,
        only=[item for item in (args.only or "").split(",") if item],
        output=args.out,
    )


if __name__ == "__main__":
    main()
