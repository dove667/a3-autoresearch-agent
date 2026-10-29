# A3 Research Agent

面向 A3 MuJoCo 项目的 AutoResearch Agent。

Agent 初始化自 mini-SWE-agent，作为第三方依赖接入本项目。目标是通过人类预设的测试反馈和评估指标，让 Agent 自主完善自身 Harness，包括工具、实验工作流和持久记忆，并逐步联合优化机械臂 Policy 与实验效率。

模型接入复用 mini-SWE-agent 的多提供商支持。执行架构默认采用本地 Agent runtime，通过 SSH 在远程 GPU 服务器上运行仿真、渲染和实验；同时规划本地执行方式，供具备兼容 GPU 和渲染环境的机器使用。A3 MuJoCo 项目在独立的 `a3_dual_arm_sim` 仓库中维护。

## 安装与启动

需要 Python 3.11+ 和 uv。在项目根目录执行：

```bash
git submodule update --init --recursive
uv sync --python 3.11
uv run mini
```

首次运行按向导配置模型服务。已自行完成认证时，可以显式指定模型和适配器启动，将下面的占位符替换为所选服务的配置：

```bash
export MODEL_NAME="<provider/model>"
export MODEL_CLASS="<model-adapter>"
MSWEA_CONFIGURED=true uv run mini --model "$MODEL_NAME" \
  --model-class "$MODEL_CLASS" -c mini.yaml -c agent.step_limit=0
```

`MODEL_CLASS` 可选 `litellm`、`litellm_response` 等 mini 已有适配器，需与所选服务兼容。启动后输入任务，让 mini 根据项目目标、规划和验收反馈逐步构建自身 Harness。

进行无人交互的 RSI 冷启动时，配置本地 `.env` 后执行：

```bash
bash scripts/start-rsi.sh
```

目标、验收、Git 工作区和轨迹保存见 [RSI 冷启动说明](docs/rsi-cold-start.md)。

模型认证、适配器与成本配置、执行环境准备见[环境配置指南](docs/setup.md)。

## 项目文档

- [开发路线与实现状态](docs/roadmap.md)
- [第三方依赖说明](third_party/README.md)
