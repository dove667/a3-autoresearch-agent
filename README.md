# A3 Research Agent

面向 A3 MuJoCo 机器人 Policy 研究的自主 Agent，持续提高 2×5、2×10 两类任务在全量评测中的入盒准确率与通过率。模型提出假设、编写研究代码、提交采集与训练作业，并根据评估结果继续实验；Harness 提供工具、skills、记忆、上下文压缩与恢复。

常规研究使用已发布的 Harness，通过文件工具准备研究代码，通过远程 Controller 执行仿真、渲染、训练和评估。每天创建新的 run 和模型上下文，共用研究工作区、计划、技能和长期经验。A3 仿真项目独立维护于 `a3_dual_arm_sim`。

## 安装与运行

需要 Python 3.11+、uv、模型服务、已配置 SSH 的执行端及兼容的 A3 环境。

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

按[环境配置](docs/guides/setup.md)准备本地记忆 embedding 模型与 llama.cpp，并填写模型认证、项目路径和远程资源配置。每天从明确的发布版本开始新一轮研究：

```bash
bash start-autoresearch.sh --harness-commit <已发布提交SHA>
```

新 run 共用根目录 `workspace/`、`plan/current.json`、`memory/` 和 `skills/`。

独立开发或体验原始 mini-SWE-agent：

```bash
uv run mini
```

常规发布版本、原地升级、夜间运行窗口及研究恢复见[使用教程](docs/guides/usage.md)。macOS 启动脚本自动使用 `caffeinate -is` 防止运行期间空闲休眠。

## 文档

- [完整文档入口](docs/README.md)
- [环境与模型配置](docs/guides/setup.md)
- [启动、恢复与版本升级](docs/guides/usage.md)
- [系统架构](docs/reference/architecture.md)
- [上下文与长期记忆](docs/reference/context-and-memory.md)
- [Controller 与远程进程作业](docs/reference/remote-process.md)
- [Agent 输入与版本管理](docs/reference/agent-inputs.md)
- [操作者维护与发布](docs/guides/maintenance.md)
- [源码和产物目录](docs/reference/storage-layout.md)
