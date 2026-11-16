"""Run the twelve tasks against one model and score what it actually did."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def post(url, key, body, attempts=7):
    """A dropped TLS session at the provider is retried, not scored as a model failure.

    A network outage can last minutes, so the backoff grows to a minute and a run
    that keeps failing stops with a clear message instead of losing finished tasks.
    """
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
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ) as error:
            last = error
            if index + 1 < attempts:
                time.sleep(min(60, 5 * 2**index))
    raise last


def ask(url, key, model, messages, tools, turns, answers=True):
    """One task conversation shaped like the real loop: state view plus drift guard.

    A live run has no "task finished" signal: prose is corrected and questions stay
    open until the Agent records a decision. The task ends when its turn budget is
    spent, so a model is never credited for stopping early.
    """
    calls = []
    invalid = []
    texts = []
    guard = drift.DriftGuard(remind=answers)
    for _ in range(turns):
        body = {
            "model": model,
            "instructions": INSTRUCTIONS,
            "input": messages + [{"role": "user", "content": OPERATIONAL}],
            "tools": tools,
        }
        response = post(url, key, body)
        output = response.json().get("output") or []
        messages.extend(output)
        texts.append(answer_text(output))
        batch = [item for item in output if item.get("type") == "function_call"]
        if not batch:
            messages.append({"role": "user", "content": drift.NO_TOOL_NOTICE})
            continue
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
            break
    return calls, invalid, " ".join(texts), guard


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
    """Explain the failure in words an operator can act on."""
    names = [call["name"] for call in calls]
    if any(forbidden in names for forbidden in task.forbid):
        return False, "调用了不该调用的工具"
    if not matches(calls, task.expect):
        unknown = [name for name in names if name not in ADVERTISED]
        if unknown:
            return False, f"编造了工具名 {unknown[0]}"
        return False, "没有做出必需的工具调用"
    if task.answer and not text.strip():
        return False, "用完轮次也没有给出结论"
    if task.answer and not all(token in text for token in task.answer):
        return False, "结论缺少必需的事实"
    return True, ""


ADVERTISED = frozenset(tool["name"] for tool in tool_catalog.TOOLS)


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


def run_task(task, url, key, model, tools, answers):
    """One task, scored from what the model actually did.

    A transport failure belongs to the harness, not to the model: it is recorded as
    an error row so one broken task cannot throw away the rest of the batch.
    """
    started = time.monotonic()
    try:
        return _run_task(task, url, key, model, tools, answers, started)
    except Exception as error:  # noqa: BLE001 — recorded, never fatal for the batch
        return {
            "id": task.id,
            "title": task.title,
            "passed": False,
            "detail": f"运行错误：{type(error).__name__}",
            "names": [],
            "calls": 0,
            "invalid": 0,
            "turns": round(time.monotonic() - started, 2),
            "drift": {},
            "answer": "",
        }


def _run_task(task, url, key, model, tools, answers, started):
    calls, invalid, text, guard = ask(
        url,
        key,
        model,
        [{"role": "user", "content": task.prompt}],
        tools,
        task.turns,
        answers=answers,
    )
    passed, detail = score(task, calls, invalid, text)
    if invalid:
        detail = f"{detail + '; ' if detail else ''}{len(invalid)} bad argument(s)"
    return {
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


def run(model=None, only=None, output=None, answers=True, jobs=4, turns=None):
    url, key, configured = endpoint()
    tools = tool_catalog.TOOLS
    selected = [
        task._replace(turns=turns) if turns else task
        for task in TASKS
        if not only or task.id in only
    ]
    directory = Path(
        output or Path("runs/checks") / f"benchmark-{time.strftime('%Y-%m-%d_%H-%M-%S')}"
    )
    results = []
    # A finished task stays finished even if the network drops on a later one.
    partial = directory / "results.json"
    if partial.exists():
        for row in json.loads(partial.read_text()).get("tasks", []):
            if any(task.id == row["id"] for task in selected):
                results.append(row)
                print(f"already done: {row['id']}", flush=True)
    done = {row["id"] for row in results}
    selected = [task for task in selected if task.id not in done]
    # Independent tasks run side by side: the provider is the only shared resource.
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {
            pool.submit(run_task, task, url, key, model or configured, tools, answers): task
            for task in selected
        }
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            print(f"{'PASS' if row['passed'] else 'FAIL'} {row['id']}: {row['detail']}", flush=True)
            report(results, model or configured, directory)
    results.sort(key=lambda row: [task.id for task in TASKS].index(row["id"]))
    summary = report(results, model or configured, directory)
    summary["answers"] = answers
    print(json.dumps({**summary, "directory": str(directory)}, ensure_ascii=False))
    return summary


def instructions_for_reference():
    """Keep the real prompt importable even though tasks use a short one."""
    return lifecycle.INSTRUCTIONS
