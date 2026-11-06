# 同一 run 的中断恢复

> 历史方案；当前执行约定以 [单研究 Agent 工作流](../research-orchestration.md) 和 [执行接口](../../agent-inputs/shared/execution.md) 为准，旧 worker、分叉和派发描述不作为当前操作指引。

目标：`--resume-from` 在原 run 目录、原时间戳、原逻辑身份和原工作区继续执行；`--run-harness` 才开启新 run。

## 实现范围

- Host 私有状态原子保存 Gateway 注册表、身份令牌、作业归属、候选关系、主身份与待决 trial；文件不挂载进 Docker，不进入公开日志。
- 中断时暂停本轮候选 Docker，保留远程作业与权限记录。正常整轮结束仍执行资源清理。
- 恢复使用原 Gateway 端口、原身份、原版本库和工作区。仍存在的进程接回；确已退出的主进程只通过自身 resume.json 恢复，不重建任务。
- Host 持有本轮进程锁，拒绝重复恢复。冻结输入与模型配置不变；新的 Host 容量上限作为部署记录维护。
- 旧运行从已停止的 Host 元数据导入原身份与关系；无法核实则拒绝，不创建新 run，不改写历史。
- 不修改 SWE 生成代码、Policy、记忆、验收或提示词；验证后按用户追加授权提交，再从最近 checkpoint 原地恢复。

## 工作项与验证

- [x] 添加私有状态存储与 run 排他锁，测试原子写入、权限及重复启动拒绝。
- [x] Gateway 保存/加载完整控制状态，测试旧主身份、候选决策权、版本读权限与远程作业归属在重建后保持，其他身份仍拒绝。
- [x] 候选 runtime 暂停/接回及同身份 checkpoint 启动，测试工作区不复制、日志不覆盖、已完成 worker 不重新执行。
- [x] 启动器分开 fresh/resume 路径，原地恢复、校验冻结输入/模型/端口与进程锁，记录恢复事件。
- [x] 同步 README、tutorial、architecture 和 Host 接口文档。
- [x] 全量 runtime 测试、Ruff、无模型 Docker 恢复检查和最终 diff 审查；不进行正式 ABCD 判定。

验证：127 项 runtime 测试通过，Ruff 与格式检查通过。无模型 Docker 验证覆盖同进程接回、同身份 checkpoint 重启和完整工作区保留；独立代码审查完成。
