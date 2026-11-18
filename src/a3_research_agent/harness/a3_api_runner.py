"""Report the callable surface of the deployed A3 package.

The research Agent writes scripts against this package from memory. Signatures
move: `train_act` sits in `learning.act_training` rather than `learning.training`,
`aggregate_datasets` takes `repo_ids`/`aggr_repo_id`/`roots=`/`aggr_root=`, and
`merge_cookie_datasets` requires two roots and a layout. Each mismatch costs a
submission and a stack trace. This launcher reads the installed source and
returns the real signatures and CLI defaults instead.

It reads files only, so it runs in seconds on a CPU job and cannot fail on an
import side effect. Run it through the `a3_api` tool.
"""

import argparse
import json
import os
import re
from pathlib import Path

SYMBOLS = (
    "audit_training_dataset",
    "train_act",
    "merge_cookie_datasets",
    "aggregate_datasets",
    "count_fill",
    "collect_episodes",
)
CLIS = (
    "workflows/benchmark_cookie_batch_cli.py",
    "data/collect_cookie_2x10_cli.py",
    "data/collect_second_round_cli.py",
    "workflows/evaluate_smolvla_grid_cli.py",
)
DEFINITION = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
FLAG = re.compile(r'["\'](--[A-Za-z0-9-]+)["\']')
DEFAULT = re.compile(r"default\s*=\s*([^,\n)]+)")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--symbols", help="Comma-separated names; defaults to the core set")
    return result


def package_root():
    import a3_dual_arm_sim

    return Path(a3_dual_arm_sim.__file__).resolve().parent


def signature(lines, start):
    """Join the definition line(s) until the parameter list closes."""
    text, depth = "", 0
    for line in lines[start : start + 12]:
        text += line
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            break
    return re.sub(r"\s+", " ", text.strip())


def definitions(root, names):
    found = {name: [] for name in names}
    for path in sorted(root.rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            match = DEFINITION.match(line)
            if match and match.group(1) in found:
                found[match.group(1)].append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": index + 1,
                        "signature": signature(lines, index)[:400],
                    }
                )
    return found


def cli_flags(root, relative):
    path = root / relative
    if not path.is_file():
        return {"file": relative, "error": "not_found"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"file": relative, "error": "unreadable"}
    flags = []
    for chunk in text.split("add_argument(")[1:]:
        block = chunk[:600]
        flag = FLAG.search(block)
        if flag is None:
            continue
        default = DEFAULT.search(block)
        flags.append(
            {
                "flag": flag.group(1),
                **({"default": default.group(1).strip()} if default else {}),
            }
        )
    return {"file": relative, "flags": flags}


def main(argv=None):
    args = parser().parse_args(argv)
    root = package_root()
    names = tuple(item.strip() for item in (args.symbols or "").split(",") if item.strip())
    report = {
        "schema": 1,
        "status": "passed",
        "package": str(root),
        "definitions": definitions(root, names or SYMBOLS),
        "cli": [cli_flags(root, relative) for relative in CLIS],
    }
    output_root = Path(os.environ.get("A3_OUTPUTS") or Path.cwd())
    relative = Path(args.output_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("--output-dir must stay below A3_OUTPUTS")
    target = (output_root / relative / "api.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"api": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
