Version: 0.1.1

本次仅检查部署准备，不执行正式 RSI，不声明 A/B/C auto research 验收通过。
先阅读 `/run/input/execution.md`、`/run/input/deployment.json` 及已有 previous-blocker.md。
使用已有 Bash 工具自主编写临时验证程序，确认以下真实链路，不要求人提供下一轮指令：

1. 通过宿主 `/sessions` 创建独立候选容器。候选使用自己的环境和身份运行验证程序，
   不能读取构建者的 `/workspace/harness`、宿主 SSH 或 Docker socket，不能写只读输入与公共 A3。
2. 在该候选中真实调用模型 gateway，提交自己的远程 CPU 作业，查询成功状态并取回文件。
   远程作业至少调用一次已有 A3 仿真接口，保存输出；不要修改 A3 或运行其 pytest。
   同时核对 `/opt/a3/docs/workflow_guide.md`、`/opt/a3/docs/contracts.md` 与 `/opt/a3/examples/benchmark_cookie_batch.py` 存在，并检查完整模块入口和绝对示例路径的 `--help` 均能运行；不要把 `/workspace` 当作 A3 根目录。
3. 由该候选创建 `/fixtures/isolation`，用自己的身份尝试查询、取消、读取异身份作业，
   保存真实拒绝响应；宿主核验异身份哨兵未变且作业仍在，随后清理 fixture。
4. 父执行取回候选的验证报告，核对它成功结束，保存模型、接口、拒绝、核验和错误证据。
   清理候选与本次远程作业。不要输出任何令牌，不安装新依赖，不运行训练或 Policy 搜索。

将逐项结果和原始证据保存到 `/workspace/run/readiness.json`，明确任何未通过项。
最终总结只报告部署是否可用及原阻塞是否已解除。完成检查后提交并结束。
