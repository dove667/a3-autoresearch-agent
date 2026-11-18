"""Catch a review that argues from a number its own route has already beaten.

Reviews are written from memory. A criterion can keep pointing at an old baseline
(``> 149/1200`` while the route now records ``279/1200``), or a conclusion can cite
a score the same route has already improved on. Both make a route look unresolved
when it is not, and neither is a matter of judgement: the numbers are recorded.
"""

import re

RATIO = re.compile(r"(\d+)\s*/\s*(\d+)")


def ratios(text):
    """Read every a/b pair, keeping the written form for the message."""
    if not isinstance(text, str):
        return []
    found = []
    for match in RATIO.finditer(text):
        found.append((int(match.group(1)), int(match.group(2)), match.group(0)))
    return found


def recorded(plan, route):
    """Collect the ratios this route has already reported, by denominator."""
    values = {}
    for review in plan.get("reviews") or []:
        if not isinstance(review, dict) or review.get("route") != route:
            continue
        for metric in review.get("metrics") or []:
            for numerator, denominator, _ in ratios(metric):
                values.setdefault(denominator, []).append(numerator)
    return values


def warnings(plan, route, conclusion, metrics, criterion=None):
    """Name a cited number that a recorded measurement already exceeds."""
    result = []
    known = recorded(plan, route)
    cited = ratios(conclusion) + [item for metric in metrics for item in ratios(metric)]
    for numerator, denominator, written in cited:
        best = max(known.get(denominator, []) or [0])
        if best > numerator:
            result.append(
                f"结论引用 {written}，但本路线已记录 {best}/{denominator}；"
                "确认引用的是最新实测，而不是旧基线"
            )
    if criterion:
        for numerator, denominator, written in ratios(criterion):
            best = max(known.get(denominator, []) or [0])
            if best > numerator:
                result.append(
                    f"判据仍是 {written}，本路线最新记录 {best}/{denominator}："
                    "判据已经低于实测，先更新判据再判断是否继续"
                )
    return sorted(set(result))
