# 研究看板

本地只读网页，把最新一次 run 的状态、研究计划、远程作业、长期记忆、工具和技能分成五个页面。深色金色界面，导航切换页面，不需要手动刷新。

## 启动与停止

看板随研究 Host 一起启动和关闭：运行 `bash start-autoresearch.sh ...` 后，Host 自动在 <http://127.0.0.1:8787> 提供页面；研究停止时页面服务一起关闭。默认端口可用 `A3_DASHBOARD_PORT` 配置。

需要单独查看已保存状态且没有研究运行时，也可以手动启动：

```bash
uv run a3-research-dashboard --open       # 启动后打开浏览器
uv run a3-research-dashboard --port 8788  # 更换本地端口
```

看板只监听回环地址。手动启动的实例使用 `Ctrl+C` 停止；研究自带的实例随研究结束自动停止。

## 页面

- **总览**：Agent 状态、当前研究重点、研究活动柱状图和路线、记忆、技能概况。状态区分未启动、启动中、运行中、等待远程作业、等待事件、整理上下文、已阻塞、已暂停、已停止和异常中断。
- **研究计划**：长期方向与路线列表。选中一条路线后，展示问题、假设、实验方法、什么结果算成功、当前阶段、按依赖顺序排列的步骤进度、当前结论，以及这条路线每次结果后的判断。
- **远程作业**：按进行中和已结束分组，只展示 Agent 在提交时填写的作业 `name` 和状态；没有 name 的历史作业回退到命令或脚本名。
- **长期记忆**：按有效、已归档、已被替代筛选，可搜索；点击条目查看全文、标签和依据。
- **Tools & Skills**：左侧按功能分组列出 Agent 当前可调用的全部高层工具，点击查看工具说明、参数类型、必填项和可选值；右侧直接展示 skills 自身的描述、版本、正文和附件信息，不添加二手介绍或标签。

每页高度跟随窗口，内容多时只在页内滚动；通过左侧导航切换，不把全部板块堆成一张长页。

## 自动更新

页面每 5 秒请求一次 `/api/snapshot`，服务端每次重新读取当前文件，并返回 `Cache-Control: no-store`。因此：

- 计划修订、记忆写入、技能更新和作业状态变化会在下一次轮询时出现；
- 新 run 出现后，“最新运行（自动）”会自动切换；
- 在运行下拉框中选定旧 run 后会固定查看该 run，选择“最新运行（自动）”恢复跟随；
- 不需要重新启动服务或手动刷新页面。

## 数据来源

看板只读以下运行资料和工具声明：

- `trajectories/latest-run.txt` 与 `trajectories/<run>/` 下的启动、Host、会话、监督、结果与作业记录；
- 项目根 `plan/current.json`；
- 根 `memory/entries.jsonl`；
- 根 `skills/<id>/SKILL.md` 与 `assets/catalog-entry.json`；
- 当前发布代码中的 `harness/tool_catalog.py` 高层工具声明。

它不读取或返回 Host 的认证令牌，不提供写接口，不修改 plan、memory、skills、工具声明或研究文件。普通页面请求失败时保留上一帧，并在顶部显示错误提示。

## 单独启动

`bash start-dashboard.sh` 检查 `A3_DASHBOARD_PORT`（默认 8787）上的 `/api/health`：已经有看板就只打印地址，没有就启动一个新的并在终端打印 URL。加 `--open` 会同时打开浏览器。研究 Host 启动时仍会自动拉起看板；若看板已经由这个脚本启动，Host 只打印 “Research dashboard already running”。

总览页显示本次运行的 token 与开销：输入、缓存命中、输出，以及按 `.env` 中 `A3_PRICE_*_PER_MTOK` 折算的金额（币种由 `A3_PRICE_CURRENCY` 标明）。旧的运行记录没有写入价格时，看板用当前单价按它们的用量补算。

## 作业状态是实时的

作业状态有两个来源。Agent 每次 `job_status` / `job_wait` 会把状态写进 `conversation/operational-state.json`；看板优先向正在运行的 Host 查询当前状态（`/sessions` 与 `/jobs`），只有查不到时才回落到 Agent 记下的快照。

- Host 内嵌的看板直接用进程内的 Gateway 客户端。
- 单独启动的看板（`start-dashboard.sh`）从 `trajectories/<run>/.host/state.json` 读取回环地址和本 run 的令牌，只用于本地读取，不写入页面。
- 查询结果缓存 10 秒，避免浏览器每 5 秒刷新就产生一次远程调用；查询失败时显示上一次已知状态，不显示错误。
