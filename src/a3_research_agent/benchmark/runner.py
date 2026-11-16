"""Run the twelve tasks against one model and score what it actually did."""

import json
import os
import time
from pathlib import Path

import httpx

from a3_research_agent.benchmark import sandbox
from a3_research_agent.benchmark.tasks import TASKS
from a3_research_agent.harness import drift, lifecycle, tool_catalog

INSTRUCTIONS = """You are the research Agent of this deployment. Use the tools to answer.
Every claim must come from a tool result. When a tool rejects a call, read its error and hint."""

OPERATIONAL = (
    '{"research_state": {"objective": "提高 A3 2×5 与 2×10 的入盒准确率与通过率", '
    '"routes": ["act-p142 active"], "steps": ["s5-closedloop-compare pending: 同种子配对比较"], '
    '"active_jobs": []}, "how_to": "文件用 read/list/search_files；作业用 job_submit/job_prepare/'
    'job_status/job_wait；步骤与复盘用 plan；既有结论用 memory；方法用 skills；原始对话用 recall。"}'
)


def endpoint():
    base = os.environ.get("A3_UPSTREAM_RESPONSES_BASE_URL", "").rstrip("/")
    key = os.environ.get("A3_UPSTREAM_RESPONSES_API_KEY", "").strip()
    model = os.environ.get("A3_UPSTREAM_RESPONSES_MODEL", "").strip()
    if not base or not key or not model:
        raise SystemExit("Set A3_UPSTREAM_RESPONSES_BASE_URL/API_KEY/MODEL before benchmarking")
    return base + "/responses", key, model


def post(url, key, body, attempts=3):
    """A dropped TLS session at the provider is retried, not scored as a model failure."""
    last = None
    for index in range(attempts):
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=httpx.Timeout(120, connect=10, read=180),
            )
            response.raise_for_status()
            return response
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as error:
            last = error
            time.sleep(2 * (index + 1))
    raise last


def ask(url, key, model, messages, tools, turns):
    """One task conversation shaped like the real loop: state view plus drift guard."""
    calls = []
    invalid = []
    text = ""
    guard = drift.DriftGuard()
    for _ in range(turns):
        response = post(
            url,
            key,
            {
                "model": model,
                "instructions": INSTRUCTIONS,
                "input": messages + [{"role": "user", "content": OPERATIONAL}],
                "tools": tools,
            },
        )
        output = response.json().get("output") or []
        messages.extend(output)
        text = answer_text(output)
        batch = [item for item in output if item.get("type") == "function_call"]
        if not batch:
            return calls, invalid, text, guard
        rejected = False
        for call in batch:
            calls.append({"name": call.get("name"), "arguments": call.get("arguments")})
            result = sandbox.execute(call, invalid)
            rejected = rejected or result.get("error") in drift.INVALID_ARGUMENT_ERRORS
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )
        notice = guard.observe(
            [{"name": call.get("name"), "arguments": call.get("arguments")} for call in batch],
            rejected,
        )
        if notice:
            messages.append({"role": "user", "content": notice})
        if guard.stalled():
            return calls, invalid, text, guard
    return calls, invalid, text, guard


def answer_text(output):
    """Collect the assistant text whatever shape the provider returns it in."""
    pieces = []
    for item in output:
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            pieces.append(item["text"])
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            pieces.append(content)
            continue
        for part in content or []:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
    return " ".join(piece for piece in pieces if piece).strip()


def matches(calls, expect):
    index = 0
    for name, predicate in expect:
        while index < len(calls):
            call = calls[index]
            index += 1
            if call["name"] != name:
                continue
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError:
                continue
            if isinstance(arguments, dict) and predicate(arguments):
                break
        else:
            return False
    return True


def score(task, calls, invalid, text):
    names = [call["name"] for call in calls]
    if not matches(calls, task.expect):
        return False, "missing expected call"
    if any(forbidden in names for forbidden in task.forbid):
        return False, "called a forbidden tool"
    if task.answer and not all(token in text for token in task.answer):
        return False, "answer lacks the required fact"
    if invalid:
        return False, "invalid arguments"
    return True, ""


def summarize(results):
    passed = sum(1 for row in results if row["passed"])
    calls = sum(row.get("calls", 0) for row in results)
    invalid = sum(row.get("invalid", 0) for row in results)
    return {
        "passed": passed,
        "total": len(results),
        "score": f"{passed}/{len(results)}",
        "tool_calls": calls,
        "invalid_arguments": invalid,
        "invalid_rate": round(invalid / calls, 3) if calls else 0.0,
    }


def report(results, model, directory):
    summary = summarize(results)
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.json").write_text(
        json.dumps(
            {"model": model, "summary": summary, "tasks": results}, ensure_ascii=False, indent=2
        )
    )
    lines = [
        f"# 能力回归测试 — {model}",
        "",
        (
            f"- 通过 {summary['score']}，工具调用 {summary['tool_calls']} 次，"
            f"参数被拒 {summary['invalid_arguments']} 次（{summary['invalid_rate']:.0%}）"
        ),
        "",
        "| 任务 | 结果 | 调用 | 说明 |",
        "|---|---|---|---|",
    ]
    for row in results:
        calls = ", ".join(row.get("names") or []) or "（无）"
        lines.append(
            f"| {row['title']} | {'通过' if row['passed'] else '失败'} | {calls} | "
            f"{row.get('detail', '')} |"
        )
    (path / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def run(model=None, only=None, output=None):
    url, key, configured = endpoint()
    tools = tool_catalog.TOOLS
    selected = [task for task in TASKS if not only or task.id in only]
    results = []
    for task in selected:
        started = time.monotonic()
        calls, invalid, text = ask(
            url,
            key,
            model or configured,
            [{"role": "user", "content": task.prompt}],
            tools,
            task.turns,
        )
        passed, detail = score(task, calls, invalid, text)
        if invalid:
            detail = f"{detail + '; ' if detail else ''}{len(invalid)} bad argument(s)"
        results.append(
            {
                "id": task.id,
                "title": task.title,
                "passed": passed,
                "detail": detail or "ok",
                "names": [call["name"] for call in calls],
                "calls": len(calls),
                "invalid": len(invalid),
                "turns": round(time.monotonic() - started, 2),
                "drift": guard.snapshot(),
                "answer": text[:400],
            }
        )
        print(f"{'PASS' if passed else 'FAIL'} {task.id}: {detail or 'ok'}", flush=True)
    directory = output or Path("runs/checks") / f"benchmark-{time.strftime('%Y-%m-%d_%H-%M-%S')}"
    summary = report(results, model or configured, directory)
    print(json.dumps({**summary, "directory": str(directory)}, ensure_ascii=False))
    return summary


def instructions_for_reference():
    """Keep the real prompt importable even though tasks use a short one."""
    return lifecycle.INSTRUCTIONS
