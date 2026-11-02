# 提示词职责重构：设计与实施记录

## 已批准设计

目标：按读者职责整理输入，而不是把禁止外部参与改成另一组禁令。用户已选择职责拆分：goal 定义研究范围，acceptance 定义证据与完成条件，protocol 定义科学比较，阶段 Prompt 定义 Agent 行动与反馈处理，execution 定义真实接口与权限；外界低频弱监督、审批及热加载操作集中到 operator 文档。

对比方案：只修正绝对禁令不能解决职责混杂；复制审批流程到 Agent 文件会让 Agent 承担外界审批。采用已批准的职责拆分，保留必要的科学摘要与完整装载正文，不引入新运行机制。

## 边界与不变量

- 只修改文档、提示词和必要离线回归；不改变 runtime、依赖、模型、权限或 input manifest。
- 不触碰历史输入、运行结果或实际实验；发布模板与运行态生效分开，不自动热加载或恢复，不连接执行机。
- 第二阶段开发可预登记配对子集；最终每快照六格 120 回合，2×5/2×10 上限 500/3200 步，G 六格等权；保持 seed 隔离、不泄漏、配对 bootstrap、Harness 至少三个配对实例与质量先行。
- 允许无收益完成；拒绝完整 Harness 不要求接任，采用仍须固定接口。保持基座优先、离线资产限制及独立任务并行但不强造改动。
- 反馈留在现有版本/来源证据链，不新增验收项，不追溯修改旧条件。每次外界热加载或中断前须用户明确批准；此责任仅写入操作者文档。

## 实施步骤

- [x] 阅读装载实现、阶段/共享输入与现有测试文字断言；现有阶段测试验证完整正文及阶段隔离，无需改动装载实现。
- [x] 增加离线职责回归，核对 operator 不注入、Agent 无 operator CLI/外界绝对禁令及核心科学约束。
- [x] 重构第二阶段输入、最小对齐第一阶段与共享 execution；所有修改的版本化输入递增版本。
- [x] 增加操作者监督文档与职责表，更新 tutorial/architecture/README 入口。
- [x] 离线验证 diff、职责回归及可用测试；记录未测项，交付审查。

## 验证范围

纯文档回归使用实际 input_sources 清单，不调用服务；在具备项目测试依赖的隔离 Python 环境中，与既有 runtime 测试一并执行；不把离线通过当作真实实验通过。重点人工复查：科学参数未变、Agent 实际可读约束未丢失、外界审批不注入、无收益闭环不被接任要求阻塞、历史与运行态不改写。

## 实施与验证结果

- goal 收拢研究范围和收益边界；执行动作移入阶段 Prompt，预算准备移入 operator 文档。
- acceptance 保留原 A/B/C/D 证据与完成标准，反馈来源/影响归入原有追溯链；明确被拒绝完整 Harness 不必接任，采用时仍走固定接口。
- protocol 保留科学数值、划分与公平比较，反馈相关部分仅约束结果可比性与历史记录；阶段 Prompt 负责核查、采纳/不采纳及保存/停止行动。
- execution 保留 API、状态、权限和加载技术事实，移除两个 operator reload CLI 教程；第一阶段只对齐反馈与参与边界，科研任务不变。
- 新增职责表与 operator-supervision；tutorial 保留原链接锚点并指向唯一操作说明，README/architecture 更新入口。input_versions.py 与其它 runtime 代码未修改。

### 版本

参与边界属于协议变化，修改的八个 Agent 输入均递增 minor 至 0.2.0：phase-one goal/acceptance（原 0.1.0）、Prompt（原 0.1.1）；phase-two goal/acceptance/protocol（原 0.1.3）、Prompt（原 0.1.8）；execution（原 0.1.12）。docs/agent-inputs 从 0.1.2 更新为 0.2.0；新增 operator 文档为 0.1.0。未修改的输入版本不变。

### 离线验证

- 新职责回归先失败，再随实现通过；最终 4 项覆盖实际 input_sources、实际发布/模型正文、外界审批/Agent 行动分离、科学不变量（完整 G、任务/步数对应、最终 120/240 与复用条件）。
- 职责/阶段输入/版本/热加载/共有指令/反馈回归合计 **40 passed**；最终全套离线测试 **257 passed**（11.53 秒）。
- 离线测试需将 mini 配置目录设为可写，并使用 litellm 的本地价格表，避免初始化时访问网络；使用 SOCKS 代理的环境需要 httpx 对应的 socksio 测试依赖。这些是测试环境准备，不修改项目依赖或运行代码。

```bash
export MSWEA_GLOBAL_CONFIG_DIR="$(mktemp -d)"
export LITELLM_LOCAL_MODEL_COST_MAP=True
export MSWEA_SILENT_STARTUP=1
python -m pytest tests -q
ruff check tests/runtime/test_prompt_responsibilities.py
git diff --check
```

- 新测试 ruff 检查通过，git diff --check 通过；修改文档本地链接路径可达，所有修改的版本化输入均核对已递增。
- 未运行真实 A3 实验、模型请求、Docker/执行服务器或热加载；离线通过不证明科研收益，也不代表运行中的输入已更新。

### 剩余风险

文字回归防止已知边界和关键科学参数漂移，不能自动证明所有自然语言无歧义；需人工与独立审查复核。发布仓库模板不等于运行中的输入已更新；实际运行干预仍按操作者文档逐次取得批准。
