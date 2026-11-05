"""Offline documentation boundaries; no service, model or container required."""

import tempfile
import unittest
from pathlib import Path

from a3_research_agent.host.inputs.versions import InputVersions, input_sources

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class PromptResponsibilitiesTests(unittest.TestCase):
    def test_operator_docs_are_not_agent_inputs(self):
        for phase in ("phase-one", "phase-two"):
            sources = input_sources(phase)
            self.assertEqual(len(sources), 8)
            self.assertEqual(sources["execution.md"], "agent-inputs/shared/execution.md")
            self.assertFalse(any(path.startswith("docs/") for path in sources.values()))
            for path in sources.values():
                text = read(path)
                for forbidden in (
                    "a3_research_agent.cli.reload_inputs",
                    "a3_research_agent.cli.reload_feedback",
                    "外部观察者不在运行中提供策略",
                    "运行中不接受人工评估标签或策略注入",
                    "外部观察者在运行中只观察",
                    "每次热加载或中断前须用户明确批准",
                ):
                    self.assertNotIn(forbidden, text, path)

    def test_operator_text_does_not_enter_published_or_model_inputs(self):
        for phase in ("phase-one", "phase-two"):
            with tempfile.TemporaryDirectory() as directory:
                versions = InputVersions(ROOT, Path(directory) / "run", phase=phase)
                versions.activate(versions.prepare(), "start")
                instructions = versions.model_instructions()[1]
                if phase == "phase-one":
                    self.assertIn(read(f"agent-inputs/{phase}/instructions.md"), instructions)
                else:
                    self.assertNotIn("phase-instructions.md", versions.contents)
                self.assertIn(read(f"agent-inputs/{phase}/goal.md"), instructions)
                self.assertIn(read(f"agent-inputs/{phase}/acceptance.md"), instructions)
                if phase == "phase-two":
                    self.assertIn(read("agent-inputs/shared/protocol.md"), instructions)
                self.assertNotIn("每次热加载或中断前", instructions)
                self.assertNotIn("父助手先说明目标 run", instructions)
                self.assertNotIn("a3_research_agent.cli.reload_feedback", instructions)

    def test_feedback_is_agent_action_not_operator_approval(self):
        for phase in ("phase-one", "phase-two"):
            text = (
                read(f"agent-inputs/{phase}/instructions.md")
                if phase == "phase-one"
                else read("agent-inputs/shared/execution.md")
            )
            if phase == "phase-one":
                for required in ("收到反馈", "采纳或不采纳", "不能", "旧条件", "来源"):
                    self.assertIn(required, text)
            else:
                # Interpretation is SOUL; evidence is acceptance C; changed
                # arrangements preserve protocol 3 rather than duplicate them.
                for required in ("human feedback", "SOUL", "固定核心提示词"):
                    self.assertIn(required, text)
                self.assertIn(
                    "反馈本身不是实验结果或通过标签", read("agent-inputs/phase-two/acceptance.md")
                )
                self.assertIn("不事后改写旧选择条件", read("agent-inputs/shared/protocol.md"))
            self.assertNotIn("用户明确批准", text)
        operator = read("docs/operator-supervision.md")
        for required in (
            "每次热加载或中断前",
            "用户明确批准",
            "低频",
            "a3_research_agent.cli.reload_inputs",
            "a3_research_agent.cli.reload_feedback",
            "--confirm",
        ):
            self.assertIn(required, operator)

    def test_research_skills_are_agent_owned_guidance(self):
        prompt = read("agent-inputs/phase-two/goal.md")
        execution = read("agent-inputs/shared/execution.md")
        for required in ("skills", "不因新任务默认重做通用能力建设", "实际加载代码"):
            self.assertIn(required, execution)
        soul = read("agent-inputs/SOUL.md")
        self.assertIn("由你判断如何使用", soul)
        self.assertIn("不授予新权限", soul)
        skill = read("skills/a3-mujoco-autoresearch/SKILL.md")
        for required in (
            "counterexamples",
            "evidence",
            "retire stale advice",
            "retained or handed off",
        ):
            self.assertIn(required, skill)
        self.assertNotIn("## 可复用研究 skills", prompt)
        with tempfile.TemporaryDirectory() as directory:
            versions = InputVersions(ROOT, Path(directory) / "run", phase="phase-two")
            proposal = versions.prepare()
            self.assertNotIn("phase-instructions.md", proposal["files"])
            versions.activate(proposal, "start")
            self.assertIn(prompt, versions.model_instructions()[1])
        operator = read("docs/operator-supervision.md")
        self.assertIn("任何人工干预", operator)
        self.assertIn("human feedback", operator)
        self.assertIn("forwarded", operator)

    def test_gpu_model_compute_boundary_is_published_in_shared_execution(self):
        execution = read("agent-inputs/shared/execution.md")
        boundary = execution.split("### 模型计算必须使用 GPU", 1)[1].split("### 作业环境与接口", 1)[
            0
        ]
        # Keep the positive requirement, unavailable-device behavior, and CPU/API
        # exceptions together in the authoritative execution layer, not only docs.
        for required in (
            "训练、微调",
            "学习型 Policy 推理和大模型推理",
            "gpu: true",
            "启动计算前确认获配 GPU",
            "模型与张量确实位于 GPU",
            "不得因租约暂不可用、CUDA 不可见或初始化失败，静默回退到 CPU",
            "等待／拒绝原因与作业状态",
            "区分正常排队和分配／可见性故障",
            "普通资源等待应退避查询",
            "报告具体阻塞及缺失条件",
            "不得绕过 controller、抢卡、取消他人作业或移除他人租约",
            "轻量元数据检查、预处理、日志分析、命令帮助与合成单元测试 fixture",
            "不能当作真实模型能力证据",
            "/v1/responses",
            "不要求调用方申请本地 GPU",
            "提供商执行不属于本地 CPU 回退",
        ):
            self.assertIn(required, boundary)
        for phase in ("phase-one", "phase-two"):
            with tempfile.TemporaryDirectory() as directory:
                versions = InputVersions(ROOT, Path(directory) / "run", phase=phase)
                versions.activate(versions.prepare(), "start")
                self.assertEqual(versions.contents["execution.md"], execution)
                self.assertIn(
                    boundary,
                    (Path(directory) / "run/inputs/execution.md").read_text(encoding="utf-8"),
                )

    def test_scientific_rules_remain_in_protocol(self):
        text = read("agent-inputs/shared/protocol.md")
        for required in (
            "120 回合",
            "| 2×5 | 20 回合 | 20 回合 | 20 回合 | 10 |",
            "| 2×10 | 20 回合 | 20 回合 | 20 回合 | 20 |",
            "2×5 每回合最多 500 个控制步，2×10 每回合最多 3200 个控制步",
            "20 Hz",
            "G = (1/6) × Σ[该格平均 cookies_in_target / 该任务单回合满分]",
            "一个 Policy 快照的完整网格为 120 回合",
            "对 P0 与最终候选各做一个完整网格，默认共 240 回合",
            "复用同一 checkpoint 的评测结果必须匹配完整协议与实际文件摘要",
            "相同快照及条件的基线结果可复用",
            "六格等权",
            "10000–10019",
            "13000–13019",
            "两集合不能重叠",
            "不得与参与训练或数据选择的场景重复",
            "配对 bootstrap",
            "95%",
            "至少完成三个配对",
            "先判交付质量",
            "无需先完成开发集 120 回合",
            "中间候选不得用最终测试结果调参",
        ):
            self.assertIn(required, text)
        acceptance = read("agent-inputs/phase-two/acceptance.md")
        self.assertIn("最终测试集对初始基线和已选定的最终 Policy 各完成 120 回合", acceptance)
        self.assertIn("同一快照和协议的结果可复用", acceptance)
        self.assertIn("无候选胜过基线", acceptance)
        self.assertIn("被拒绝的完整候选不要求接任", acceptance)
        prompt = read("agent-inputs/phase-two/goal.md")
        self.assertIn("优先适配、微调", prompt)
        self.assertIn("不自行下载", prompt)
        soul = read("agent-inputs/SOUL.md")
        self.assertIn("避免重复提交", soul)
        self.assertNotIn("在等待其结果的时间里优先", prompt)
        allocation = read("skills/rsi-harness-evolution/references/policy-harness-allocation.md")
        self.assertIn("validation", allocation)
        self.assertIn("remaining budget", allocation)
        self.assertIn("Do not rewrite Host-owned SOUL", allocation)


if __name__ == "__main__":
    unittest.main()
