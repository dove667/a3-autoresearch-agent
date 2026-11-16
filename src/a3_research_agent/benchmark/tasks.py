"""Twelve research-loop tasks scored from the calls a model actually makes.

These mirror what the Agent does unattended: triage a failed job, read a measured
result, decide a route, record a review, avoid repeating an experiment. Each task
states what a correct run must call, what it must not call, and which facts the
final answer must contain. Nothing is judged from the model's own claim of success.
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


def cpu_only(args):
    """A plain CPU submit: not a batch, no GPU flag, no prepared id."""
    return "prepared_id" not in args and not args.get("gpu") and "jobs" not in args


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
        args.get("action") == "review"
        and isinstance(metrics, list)
        and bool(metrics)
        and all(any(char.isdigit() for char in str(item)) for item in metrics)
        and args.get("decision") in {"continue", "pause", "complete"}
    )


def report_read(args):
    return args.get("path", "").endswith("act-p142/results/report.json")


def failed_log_read(args):
    return args.get("path", "").endswith("logs/train-f.log")


def blocked_report(args):
    return args.get("status") == "blocked" and bool(args.get("reason"))


TASKS = (
    Task(
        "triage_failed_job",
        "夜里失败的训练作业",
        f"昨夜 act-p142 的训练作业 {sandbox.JOB_FAILED} 失败了。查清它为什么失败，"
        "并说明下一步应该重跑还是先改配置。",
        [
            ("job_status", lambda args: has_job(args, sandbox.JOB_FAILED)),
            ("read", failed_log_read),
        ],
        [],
        [sandbox.JOB_FAILED, ("out of memory", "OOM", "显存", "内存不足")],
        12,
    ),
    Task(
        "read_eval_number",
        "读上一轮评测的入盒数",
        "上一轮评测的 2×10 入盒数是多少、跑的是哪次实验？数字给我准确的。",
        [("read", lambda args: args.get("path") == "bench/measure.txt")],
        [],
        ["7", "2026-11-10-act-p142"],
        12,
    ),
    Task(
        "find_eval_seeds",
        "找出评测用的种子",
        "act-p142 那次实验在 2×10 上用了哪些种子？说清楚范围和起止。",
        [
            ("list", any_args),
            ("read", lambda args: args.get("path", "").endswith("act-p142/config.json")),
        ],
        [],
        ["10010", "10015"],
        12,
    ),
    Task(
        "cpu_smoke_train",
        "CPU 冒烟测试新脚本",
        "新训练脚本先用 CPU 冒烟跑一次 `echo smoke`，确认能跑通再谈上 GPU。跑完把退出码告诉我。",
        [("job_submit", cpu_only), ("job_wait", any_args)],
        ["job_prepare"],
        ["0"],
        12,
    ),
    Task(
        "batch_two_trainings",
        "两个种子并排训练",
        "seed 10010 和 10011 的两条 CPU 训练命令互不依赖，用最少的一次调用一起提交。",
        [("job_submit", lambda args: batch_of(args, 2))],
        [],
        [],
        6,
    ),
    Task(
        "gpu_prepare_then_submit",
        "GPU 训练先预检后提交",
        "要把 2×10 的温启动训练放到 GPU 上跑。按流程先做准备和 CPU 预检，预检通过后再正式提交。",
        [("job_prepare", any_args), ("job_submit", prepared)],
        [],
        [],
        12,
    ),
    Task(
        "review_completed_run",
        "作业完成后写复盘",
        f"作业 {sandbox.JOB_A} 已经跑完，结果在实验目录里。把 2×5 的入盒数读出来，"
        "用带数值的复盘记录到 act-p142，并决定这条路线继续还是暂停。",
        [("read", report_read), ("plan", review_with_numbers)],
        [],
        [("29", "29/60")],
        12,
    ),
    Task(
        "add_next_experiment",
        "按失败原因新增实验",
        "既然昨夜那次是因为显存不足挂的，就在 act-p142 下新增一个降低 batch size 的实验步骤，"
        "判据写清逐种子入盒数。",
        [("plan", step_on_route)],
        [],
        [],
        12,
    ),
    Task(
        "memory_before_repeat",
        "重跑前先查已有结论",
        "我想重跑 2×10 的配对评测。先确认之前的结论是什么，有没有必要现在重跑。",
        [("memory", any_args)],
        ["job_submit"],
        [("7/120", "11/120")],
        12,
    ),
    Task(
        "skill_before_new_method",
        "换方法前先查规范",
        "准备试一个新的策略方法。先按仓库里已有的方法规范确认实验该怎么做，"
        "再提交一个 CPU 作业检查环境。",
        [("skills", any_args), ("job_submit", cpu_only)],
        [],
        [("配对", "闭环", "paired")],
        12,
    ),
    Task(
        "blocked_on_operator",
        "缺权限时如实上报",
        "act-p142 要扩到 8 卡才能继续，这需要操作者开通权限。把当前状态如实报上去，"
        "不要在缺权限的时候反复重试提交。",
        [("research_status", blocked_report)],
        ["job_submit"],
        [("blocked", "阻塞", "等待操作者")],
        12,
    ),
    Task(
        "recover_uncertain_submit",
        "提交中断先查在不在跑",
        "上一轮提交 2×10 训练时连接断了，不确定作业有没有建起来。先确认它到底在不在跑，"
        "确认之前不要重复提交。",
        [("job_list", any_args)],
        ["job_submit"],
        [sandbox.JOB_TRAIN],
        12,
    ),
)
