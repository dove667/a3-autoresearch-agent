"""Offline documentation boundaries; no service, model or container required."""

import tempfile
import unittest
from pathlib import Path

from runtime.local.input_versions import InputVersions, input_sources

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class PromptResponsibilitiesTests(unittest.TestCase):
    def test_operator_docs_are_not_agent_inputs(self):
        for phase in ("phase-one", "phase-two"):
            sources = input_sources(phase)
            self.assertEqual(len(sources), 9 if phase == "phase-two" else 8)
            self.assertEqual(sources["execution.md"], "agent-inputs/shared/execution.md")
            self.assertFalse(any(path.startswith("docs/") for path in sources.values()))
            for path in sources.values():
                text = read(path)
                for forbidden in (
                    "runtime.local.reload_inputs",
                    "runtime.local.reload_feedback",
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
                self.assertIn(read(f"agent-inputs/{phase}/instructions.md"), instructions)
                self.assertIn(read(f"agent-inputs/{phase}/goal.md"), instructions)
                self.assertIn(read(f"agent-inputs/{phase}/acceptance.md"), instructions)
                if phase == "phase-two":
                    self.assertIn(read("agent-inputs/phase-two/protocol.md"), instructions)
                self.assertNotIn("每次热加载或中断前", instructions)
                self.assertNotIn("父助手先说明目标 run", instructions)
                self.assertNotIn("runtime.local.reload_feedback", instructions)

    def test_feedback_is_agent_action_not_operator_approval(self):
        for phase in ("phase-one", "phase-two"):
            text = read(f"agent-inputs/{phase}/instructions.md")
            for required in ("收到反馈", "采纳或不采纳", "不能", "旧条件", "来源"):
                self.assertIn(required, text)
            self.assertNotIn("用户明确批准", text)
        operator = read("docs/operator-supervision.md")
        for required in (
            "每次热加载或中断前",
            "用户明确批准",
            "低频",
            "runtime.local.reload_inputs",
            "runtime.local.reload_feedback",
            "--confirm",
        ):
            self.assertIn(required, operator)

    def test_research_skills_are_agent_owned_guidance(self):
        prompt = read("agent-inputs/phase-two/instructions.md")
        for required in (
            "## 可复用研究 skills",
            "skills/INDEX.md",
            "外部参考／待验证",
            "本地验证",
            "失效或被反证",
            "不要求每轮新增 skill",
            "不能覆盖当前目标、验收、科学协议或固定权限",
            "continuation.json",
            "恢复清单",
            "不得把新增 skill 数量当作研究收益",
            "检查实际注册与可调用状态，再决定复用或修补",
        ):
            self.assertIn(required, prompt)
        with tempfile.TemporaryDirectory() as directory:
            versions = InputVersions(ROOT, Path(directory) / "run", phase="phase-two")
            proposal = versions.prepare()
            self.assertEqual(proposal["files"]["phase-instructions.md"]["version"], "0.7.0")
            versions.activate(proposal, "start")
            self.assertIn(prompt, versions.model_instructions()[1])
        operator = read("docs/operator-supervision.md")
        self.assertIn("任何人工干预", operator)
        self.assertIn("human feedback", operator)
        self.assertIn("forwarded", operator)

    def test_scientific_rules_remain_in_protocol(self):
        text = read("agent-inputs/phase-two/protocol.md")
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
        prompt = read("agent-inputs/phase-two/instructions.md")
        for required in ("不为填满等待时间强行制造改动", "优先微调", "离线资产"):
            self.assertIn(required, prompt)


if __name__ == "__main__":
    unittest.main()
