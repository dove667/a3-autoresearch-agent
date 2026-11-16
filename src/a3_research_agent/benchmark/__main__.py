"""CLI: python -m a3_research_agent.benchmark [--model NAME] [--only id,id] [--out DIR]."""

import argparse

from a3_research_agent.benchmark.runner import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override the configured upstream model")
    parser.add_argument("--only", help="Comma-separated task ids")
    parser.add_argument("--out", help="Output directory under runs/checks")
    parser.add_argument("--jobs", type=int, default=12, help="Tasks to run side by side")
    parser.add_argument("--turns", type=int, help="Override every task's turn budget")
    parser.add_argument(
        "--no-answer-budget",
        action="store_true",
        help="Disable the demanded text answer, for an A/B against the old behaviour",
    )
    args = parser.parse_args()
    run(
        model=args.model,
        only=[item for item in (args.only or "").split(",") if item],
        output=args.out,
        answers=not args.no_answer_budget,
        jobs=args.jobs,
        turns=args.turns,
    )


if __name__ == "__main__":
    main()
