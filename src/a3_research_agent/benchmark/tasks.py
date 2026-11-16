"""Twelve fixed tasks scored from the calls a model actually makes.

Each task states what a correct run must call, what it must not call, and which
facts the final answer must contain. Nothing is judged from the model's own claim
of success.
"""

from collections import namedtuple

from a3_research_agent.benchmark import sandbox

Task = namedtuple("Task", "id title prompt expect forbid answer turns")


def any_args(args):
    return True


def has_job(args, job_id):
    return args.get("job_id") == job_id


def batch_of(args, size):
    jobs = args.get("jobs")
    return isinstance(jobs, list) and len(jobs) == size


def prepared(args):
    return args.get("prepared_id") == sandbox.PREPARED


def step_on_route(args):
    steps = args.get("steps")
    return (
        isinstance(steps, list)
        and bool(steps)
        and all(isinstance(item, dict) and item.get("route") == "act-p142" for item in steps)
    )


def review_with_numbers(args):
    metrics = args.get("metrics")
    return (
        isinstance(metrics, list)
        and bool(metrics)
        and all(any(char.isdigit() for char in str(item)) for item in metrics)
        and args.get("decision") in {"continue", "pause", "complete"}
    )


TASKS = (
    Task(
        "read_number",
        "读取文件并报数",
        "把 bench/measure.txt 里的入盒数报给我，并说明它来自哪次评测。",
        [("read", lambda args: args.get("path") == "bench/measure.txt")],
        [],
        ["7", "2026-11-10-act-p142"],
        12,
    ),
    Task(
        "list_then_read",
        "先定位再读文件",
        "experiments 目录下 2026-11-10-act-p142 的 config.json 里记录的 2×10 种子是哪些？",
        [
            ("list", any_args),
            ("read", lambda args: args.get("path", "").endswith("act-p142/config.json")),
        ],
        [],
        ["10010", "10015"],
        12,
    ),
    Task(
        "job_exit_code",
        "按编号查退出码",
        f"作业 {sandbox.JOB_A} 的退出码是多少？",
        [("job_status", lambda args: has_job(args, sandbox.JOB_A))],
        [],
        ["0"],
        12,
    ),
    Task(
        "cpu_submit_wait",
        "提交 CPU 作业并等待",
        "提交一个 CPU 作业执行 `echo hello`，等它结束后把退出码告诉我。",
        [("job_submit", any_args), ("job_wait", any_args)],
        ["job_prepare"],
        ["0"],
        12,
    ),
    Task(
        "batch_submit",
        "一次提交两个作业",
        "有两个互不依赖的 CPU 命令要跑（`echo a` 和 `echo b`），用最少的调用一起提交。",
        [("job_submit", lambda args: batch_of(args, 2))],
        [],
        [],
        4,
    ),
    Task(
        "gpu_preflight",
        "GPU 先预检后提交",
        "要提交一个 GPU 训练作业。先做准备和 CPU 预检，预检通过后才正式提交。",
        [("job_prepare", any_args), ("job_submit", prepared)],
        [],
        [],
        12,
    ),
    Task(
        "plan_blocked",
        "说出未完成步骤",
        "计划里现在还有哪些没完成的步骤？分别要做什么？",
        [("plan", any_args)],
        [],
        ["s5-closedloop-compare"],
        12,
    ),
    Task(
        "plan_add_step",
        "新增步骤挂到路线",
        "在路线 act-p142 下新增一个步骤：用固定开发种子做 DAgger 温启动 checkpoint 的配对评测，"
        "判据是逐种子入盒数与原生通过。",
        [("plan", step_on_route)],
        [],
        [],
        12,
    ),
    Task(
        "plan_review_metrics",
        "复盘并给出数值",
        "复盘 act-p142：2×10 入盒从 11/120 掉到 7/120，2×5 从 14/60 升到 29/60。写下结论并决定下一步。",
        [("plan", review_with_numbers)],
        [],
        [],
        12,
    ),
    Task(
        "memory_recall",
        "检索既有结论",
        "之前 2×10 配对评测的结论是什么？",
        [("memory", any_args)],
        [],
        ["11/120"],
        12,
    ),
    Task(
        "skill_method",
        "先查方法再用",
        "做配对闭环评测应该按什么口径？",
        [("skills", any_args)],
        [],
        ["配对"],
        12,
    ),
    Task(
        "no_duplicate",
        "不重复提交已取消作业",
        f"作业 {sandbox.JOB_CANCELLED} 已经被取消了，我现在想拿到它的结果，该怎么办？",
        [("job_status", lambda args: has_job(args, sandbox.JOB_CANCELLED))],
        ["job_submit"],
        [],
        12,
    ),
)
