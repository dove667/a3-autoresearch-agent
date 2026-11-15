# 网页研究用量：取消 run 累计次数上限

Version: 0.1.0

> 本文记录取消累计上限时的实现。后续正文读取已切换到 [Tavily Basic Extract](2026-11-02-tavily-proxy-extract.md)；下文原直接 HTTP 抓取／SSRF 回归说明保留为历史，不表示当前提供商抓取由 Host 独立验证目标 DNS。无累计上限和持久尝试计数的规则继续有效。

## 决定与范围

用户明确指出：只要还有提供商额度，单个 run 应能持续有用的 auto research，而不是被 100 次搜索/200 次 fetch 人为截断。此更正取代[原网页网关方案](2026-11-02-research-web-gateway.md)的累计上限设计；只修改代码、测试及文档，不运行真实搜索或实验，不更改凭据、部署或当前 run。

- 移除 WebConfig 的 search_limit/fetch_limit；旧环境变量忽略，已配置 key 无需改动。
- 保留持久、跨身份/恢复共享的尝试计数和失败计数；跨进程加锁、原子落盘，损坏状态失败关闭。旧有限 limit 迁移为 null，used 与历史事件不清零。
- 为兼容继续使用 budget 文件/响应/审计名称：每项 `{limit:null,used:<累计尝试数>}`，status 增加 `no_run_cap:true`；不是账户余额或余额估计。
- 保留默认关闭、并发 2、basic 每次 1 credit、超时、参数/内容大小边界、SSRF/TLS/认证保护、远程离线规则。不新增自动重试、账户余额轮询、购买或升级；计费由用户控制，不保证免费。
- 按 [Tavily 官方 Search 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)（2026-11-02 核验）：401 → provider_auth_error；429 → provider_rate_limited；432/433 → provider_quota_exhausted；其他 → provider_error。限流建议有限退避，额度错误在额度或配置改变前不重复请求；其他离线工作继续。434 表示实时内容暂不可用，本接口不启用该功能。仅返回安全网关 request_id，不返回原始提供商错误或密钥。

接口 execution 与阶段二 Prompt 版本从 0.4.0 升为 0.5.0。代码不自动部署，运行干预仍逐次批准。

## 验证

离线 fake provider/HTTP 测试覆盖超过 100 次搜索和 200 次 fetch、旧满额计数迁移/恢复、跨实例并发计数、损坏状态失败关闭、错误区分/脱敏/不重试；保留原认证、SSRF、大小及超时回归。不宣称真实账户或网络已验证。

本地验证：新增/改写测试先运行得到 11 个预期失败；实现后网页专项 51 passed。全量首次 310 passed / 4 failed（均为恢复测试的旧 limit:100 断言），同步接口预期后最终 **314 passed（14.02 秒）**；`ruff check .` 和 `git diff --check` 通过。命令使用 `/tmp/a3-review-venv`，设置 `MSWEA_GLOBAL_CONFIG_DIR=/tmp/a3-mini-config`、`LITELLM_LOCAL_MODEL_COST_MAP=True`、`MSWEA_SILENT_STARTUP=1` 与 `PYTHONPATH=.`；没有真实 API 调用或研究 run。
