# Tavily API 专用本地代理与提供商正文提取

Version: 0.1.0

## 范围

搜索保持 Tavily basic；`/web/fetch` 保留 `url`／`max_chars` 参数与来源结果外形，改由 Tavily Basic Extract 读取网页。Host 只连接固定 `api.tavily.com`，不直接解析／连接用户给定的网站，不添加本地浏览器、JavaScript 或全局网络改造。原公开 HTTP 抓取计划保留为历史，已被本方案替代。

可选 `A3_TAVILY_HTTP_PROXY=http://127.0.0.1:1082` 沿用现有 Shadowrocket HTTP 代理，仅影响 Tavily API search／extract。只接受无凭据数字回环 HTTP 地址；CONNECT 目标固定为 `api.tavily.com:443`，不发送认证头，隧道内仍验证 Tavily TLS 证书与域名，API key 留在 TLS 内。不允许 Agent 指定代理、任意上游或代理头，不更改系统／全局代理。现有 `.env` key 保留。

目标 URL 仅做词法筛选，拒绝 IP 字面量、明显本地名称、凭据和不允许的端口。此筛选不等同于独立目标 DNS／重定向验证，目标抓取安全由提供商负责；不把旧 Host SSRF 固定 IP 保护描述成新提取路线的保障。

## 用量与职责

`provider` 为 `tavily`，fetch 后端为 `tavily_extract`；不提供原始 HTTP 页面或通用下载。Basic Extract 每 5 个成功提取 URL 消耗 1 credit，失败 URL 提取不收费；本地失败不一定表示提供商未成功处理。搜索每次 1 credit（[官方额度说明](https://docs.tavily.com/documentation/api-credits)，2026-11-02 核验）。本地累计尝试不退款、无每 run 总次数上限、不查询余额、不自动启用付费的语义不变。

Harness 仍自行实现、注册、执行与验证工具；E-only、历史 A–D 带来源继承、各代责任、固定科学验收、远程离线、来源不可信及私密信息保护规则不变。不是重启 E 的授权，也不自动修改运行中的输入。

## 验证与发布边界

后续已批准增加同项目跨 run 的 Host 私有结果缓存：500,000,000 字节 LRU 容量、search 24 小时、fetch 7 天，分别由 `A3_WEB_CACHE_MAX_BYTES`、`A3_WEB_SEARCH_TTL_SECONDS`、`A3_WEB_FETCH_TTL_SECONDS` 配置；容量 0 关闭。只缓存成功公开搜索结果和提取正文，不保存 key 或原始查询元数据。锁／目录有少量额外磁盘开销，容量不保证精确物理占用或页数。`refresh:true` 显式重新访问提供商；命中不产生上游请求，过期不静默回退。原始 retrieved_at 与本次 served_at 分开，used 与保守的上游请求尝试数 provider_requests 分开，均不是账户余额或 run 总次数限制。

实现回归用模拟提供商与代理覆盖固定上游、TLS、认证保密、URL 词法拒绝、成功正文／空或失败提取、响应大小、超时及安全错误；不靠离线测试声称真实 API 或 E 验收通过。本方案不启动实验、下载模型或自动发起真实 API；经批准的最小实测与后续 E 启动分别记录实际证据。

离线验证记录：431 项测试通过（含 18 项缓存专项），Ruff 与 git diff --check 通过；独立安全复查无阻断。未使用真实 key 或修改全局代理／DNS；这些离线结果不代表真实 API 或内部 Agent 的 E 验收通过。
