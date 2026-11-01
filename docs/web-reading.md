# 网页读取与百科搜索（C00.1 / C01.1 / C01.2）

当前范围：独立网页读取、可选主/子读取工具，以及独立 English Wikipedia 搜索服务/CLI。搜索只覆盖该百科，不是通用互联网搜索；搜索工具闭环、模型自动选择来源与引用质量仍为 C01.3/4。浏览器另见 [浏览器观察](browser-observation.md)。

## 使用

在项目目录使用现有 Python 环境运行：

```powershell
python -m adam.web https://example.com/
```

输出 JSON 包含 `schema_version=1`、`requested_url`、`final_url`、`fetched_at`（UTC）、`sha256`（原始实体摘要）、`byte_size`、`title`、`text`、`content_type`、`truncated`、`redirects`。命令不创建模型客户端、会话或调用付费 API。成功返回 0；受控失败返回 1，输出安全的 `error_type`；中断返回 130。

程序接口：`WebReader(transport, policy, limits).fetch(url, cancel=token)`。调用方拥有 `HttpPageTransport` 并须在 finally 关闭；服务只借用窄 `PageTransport`。不传全局配置或 SessionStore。

默认上限：实体 1 MiB、正文 16000 字符、4 次重定向、完整抓取期限 15 秒。字符截断有显式标记；字节超限不返回快照。默认 UTF-8，采用响应声明的 charset，非法字节替换为 U+FFFD，不承诺自动检测历史网页编码。

## 网络与资料边界

- 仅允许 HTTP(S) 公共地址和 80/443 端口；拒绝 URL 凭据、控制字符、内网、环回、链路本地、映射 IPv4、保留/组播地址。DNS 全部结果均须合规，不能只验证第一个。
- 每次连接与重定向重新检查，固定已验证 IP 连接，保留原始 Host/TLS SNI 与证书验证；禁用 keepalive，避免多个域名共用 IP 连接而绕过 TLS 主机验证。无自动重试、代理、认证或跨请求 cookies。
- 测试仅由可信宿主注入精确的环回 origin，普通 CLI 没有内网放行选项。这不是防火墙替代品，部署仍应限制出口。
- 仅接受 HTML/XHTML/纯文本。请求 identity 并拒绝压缩响应，避免解压炸弹；不执行 JavaScript，不读取子资源。HTMLParser 去掉脚本/样式等，不能替代完整浏览器可见性计算，动态网站交给 C02。
- 网页内容始终是不可信资料，不授予执行权限。此阶段只返回不可变来源快照；来源持久化、片段引用与聊天闭环由 C01.3 完成。
- 超时涵盖抓取等待；取消会终止异步连接和读取。系统 DNS 使用平台 resolver，原生 DNS 调用若阻塞，事件循环关闭可能等待其返回，不能承诺操作系统 DNS 的硬取消时限。无自动重发。

## 可选工具接入（C00.1）

主配置按需添加（示例配置已包含默认关闭项）：

```toml
[web]
enabled = true
max_bytes = 1048576
max_chars = 4000
max_redirects = 4
timeout_seconds = 15
```

启用表示允许原工具管线读取策略内的公共网页，不放宽文件/命令权限。默认关闭时不注册 `read_webpage`、不导入网页后端；开启但普通聊天不读取时也不创建连接。当前后端复用项目已有 HTTPX，无新增必装 SDK。配置没有私网放行或代理绕过键。

默认按需发现：先 `discover_tools(names=["read_webpage"])`，在后续模型请求调用 `read_webpage(url=...)`。工具返回 `status=fetched` 及完整 `source` 结构；正文可进一步限长，但来源 URL/摘要不会被通用预览悄悄替换。连元数据都放不下时返回 `output_limit`，不能视为有效引用；抓取失败以 `error_type` 分类。独立百科搜索见下文；搜索工具接入及稳定来源 ID/片段映射仍属于 C01.3。

主任务与子任务使用同一注册函数。只读声明、发现名单、模式、工具总次数、Hook、日志、取消及录制重放均复用现有管线。每个实际调用在所属工作线程通过 `open_reader` 创建/关闭独立资源，支持两个安全读取并发，不共享 HTTP 客户端、cookies 或事件循环；启动/闲聊没有需长期持有的网页资源。构造失败也释放已创建部分，清理失败给原异常加安全说明而不覆盖它。若系统 DNS 阻塞导致读取线程不能及时退出，沿用既有 ToolCleanupError 停止后续派发，不宣称后端已硬取消。

验收入口：`python -m pytest -q tests/features/test_optional_web.py tests/features/test_web_reading.py tests/test_web_config.py tests/test_web_policy.py tests/test_architecture.py`，97 项通过（新增 13 项业务、11 项配置）。模型 HTTP 使用 MockTransport，但网页服务、主子线程、Hook 检查器、JSONL、关闭与取消均实际执行；离线重放零重新连接。原 C01.1 的 21 个场景迁移共享夹具后没有遗漏或重复。

本机 pytest 9.1.1 的 conftest 按目录节点匹配；同次命令交错重复进入父/子目录时可能出现同名目录节点而漏匹配夹具。定向命令将同目录文件放在一起，全量用单一 `tests`/默认 testpaths 入口；未修改 pytest、未跳过测试、未改变权限。已核对全仓 1890 项收集结果及单入口夹具可见性。

2026-11-01 12:13（Asia/Shanghai）新 CLI 已真实读取 `https://example.com/`，标题 Example Domain、实体 577 字节、SHA-256 `25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484`；TLS SNI/证书验证保持开启。该一次后端验证不证明搜索或模型效果，不作为日常联网自动测试。

## 验收

`python -m pytest -q tests/features/test_web_reading.py tests/test_web_policy.py tests/test_architecture.py`：73 项通过。故障注入只在 DNS 边界模拟解析失败/重绑定，其余连接、重定向、正文、超时和取消均为真实本地 ThreadingHTTPServer，不使用 MockTransport。没有迁移或删除已有场景。

Ruff format/lint、Pyright 与全量 pytest 是每项提交门禁。下文的真实百科后端验证不证明通用互联网覆盖或模型效果；C01.3/4 保持待办。


## 独立百科搜索（C01.2）

```powershell
python -m adam.web.search "Nelson Mandela" --backend wikipedia --limit 5
```

这是首个无需密钥的真实后端，范围仅为 English Wikipedia。选型原因：本机可直接访问官方 [MediaWiki Search API](https://www.mediawiki.org/wiki/API:Search)，而此前 DuckDuckGo HTML 可用性探测返回 202 挑战；不绕过挑战、不把不可用后端或百科范围声称为通用互联网检索。首版不注册搜索模型工具、不增加 AppConfig 开关，C01.3 再接入原发现/权限/日志入口；C01.4 的时效、冲突、无答案、重定向及无需搜索固定任务与真实模型判读仍须完成，不能由本项代替。

- 后端须显式选择；省略 `--backend` 输出 `not_configured` 并返回 1，尚未创建 HTTP 资源。合法无结果则返回 0、`ok=true`、空 `candidates`。未知程序化后端为 `unsupported_backend`。本后端不需要凭据，也不读取模型密钥；未来有凭据后端只能由适配器消费。
- 默认至多 5 条，允许 1～20 条；查询 512 字符（上限 1024），标题 512、摘要 768 字符。响应 1 MiB、总期限 15 秒；限额可由可信宿主通过 `SearchLimits` 注入，CLI 仅暴露结果数量。空白/控制字符/孤立代理字符、原文或 URL 编码后超长查询在发送前拒绝；标题/摘要截断分别显式标记。字节超限、超量/重复/非法条目使整批失败，不返回部分候选或旧缓存。
- 输出包含 `provider`、`scope`、规范化 `query`、UTC `searched_at`、`endpoint`、`response_sha256`、`response_byte_size`、`candidates` 和 `has_more`。`evidence_kind=search_snippets` 明确它不是正文；摘要只是选择下一次读取的线索，响应摘要只标识 API 实体，不能冒充候选页面内容版本。`has_more` 来自实际 continuation，不承诺全量召回，首版不自动翻页。
- API 固定在官方端点；本地验收替代仅由宿主显式注入精确环回 origin。请求正文搜索、主名字空间及 snippet；候选 URL 从可信 origin 和合法数值 page ID 构造，忽略远端任意 URL。高亮标签移除但不在词内插空格；脚本/样式/模板等排除。保留的资料仍不可信，不根据摘要中的指令执行动作。
- 搜索与读取共用 `PageTransport`、DNS 全地址检查、固定 IP、Host/SNI、证书验证、identity/字节上限、无代理/认证/cookies/重试规则。搜索显式注入只允许 JSON 的传输配置，原网页传输默认类型与 WebReader 的 HTML/纯文本校验不变。API 重定向直接拒绝，既不跟随同源目标也不跟随私网目标。
- `rate_limited` 覆盖 HTTP 429 及 JSON `ratelimited`/`maxlag`；其他非 200 为 `http_NNN`，API error/warning、非法 UTF-8/JSON/响应结构、错误 MIME、压缩、超时及配置缺失分别分类。错误不回显查询、远端错误正文或内部诊断，失败与中断不重发。
- 程序接口是 `WikipediaSearch(transport, policy, limits).search(query, cancel=token)`；服务借用端口，调用方负责关闭。普通使用经 `open_search("wikipedia", limits, policy)` 按次创建并关闭 HTTP 资源；构造、查询、取消失败均释放，清理故障不覆盖原异常。期限包括返回前校验，JSON/摘要解析是有界同步工作，取消在阶段与条目间检查；不承诺同步解析或平台 DNS 的硬抢占时限。

### 本项验收

新增 [27 项业务](../tests/features/test_web_search.py) 和 [69 项协议/工厂边界](../tests/test_web_search.py)。业务用临时本地 ThreadingHTTPServer，实际经过 DNS/HTTP/解析、来源版本、输出限额、取消、cookies/代理隔离及 CLI；只有远端 API 被本地服务替代。低层错误组合使用窄响应端口注入非法协议/晚到响应/清理失败，不用它证明真实网络。原网页场景未迁移或删除。

联合命令：`python -m pytest -q tests/features/test_web_search.py tests/features/test_web_reading.py tests/features/test_optional_web.py tests/test_web_search.py tests/test_web_policy.py tests/test_web_config.py tests/test_architecture.py`。该联合命令 195 项通过；全仓格式/lint、Pyright 和 pytest 作为独立提交门禁。

2026-11-01 17:20（Asia/Shanghai）最终代码通过新 CLI 实际发出两次免费只读搜索：`Nelson Mandela` 返回 2 条，随机无匹配词返回 0 条；实际 CLI 耗时约 1.062 / 0.860 秒。输出、URL、响应摘要、源码摘要和限制保存于 [真实后端记录](evaluations/c01-search-backend.json)。这不是日常联网自动测试，没有付费模型调用，不证明模型选择工具、答案引用质量、通用搜索范围或持续可用性。
