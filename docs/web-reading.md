# 网页读取与百科搜索（C00.1 / C01.1～4）

当前范围：独立网页读取、可选主/子读取工具，以及独立 English Wikipedia 搜索服务/CLI 与主子搜索→阅读→片段引用工具闭环。搜索只覆盖该百科，不是通用互联网搜索；C01.4 已完成固定真实对照，严格完整通过 2/5、另 1 项部分通过，质量边界见文末。浏览器另见 [浏览器观察](browser-observation.md)。

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
- 网页内容始终是不可信资料，不授予执行权限。返回不可变来源快照，C01.3 通过原 JSONL 保存来源并提供逐字片段回查；不根据网页中的指令扩大授权。
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

默认按需发现：先 `discover_tools(names=["read_webpage"])`，在后续模型请求调用 `read_webpage(url=...)`。工具返回 `status=fetched` 及完整 `source` 结构；正文可进一步限长，但来源 URL/摘要不会被通用预览悄悄替换。连元数据都放不下时返回 `output_limit`，不能视为有效引用；抓取失败以 `error_type` 分类。独立百科搜索及 C01.3 的搜索工具/快照片段引用见下文。

主任务与子任务使用同一注册函数。只读声明、发现名单、模式、工具总次数、Hook、日志、取消及录制重放均复用现有管线。每个实际调用在所属工作线程通过 `open_reader` 创建/关闭独立资源，支持两个安全读取并发，不共享 HTTP 客户端、cookies 或事件循环；启动/闲聊没有需长期持有的网页资源。构造失败也释放已创建部分，清理失败给原异常加安全说明而不覆盖它。若系统 DNS 阻塞导致读取线程不能及时退出，沿用既有 ToolCleanupError 停止后续派发，不宣称后端已硬取消。

验收入口：`python -m pytest -q tests/features/test_optional_web.py tests/features/test_web_reading.py tests/test_web_config.py tests/test_web_policy.py tests/test_architecture.py`，97 项通过（新增 13 项业务、11 项配置）。模型 HTTP 使用 MockTransport，但网页服务、主子线程、Hook 检查器、JSONL、关闭与取消均实际执行；离线重放零重新连接。原 C01.1 的 21 个场景迁移共享夹具后没有遗漏或重复。

本机 pytest 9.1.1 的 conftest 按目录节点匹配；同次命令交错重复进入父/子目录时可能出现同名目录节点而漏匹配夹具。定向命令将同目录文件放在一起，全量用单一 `tests`/默认 testpaths 入口；未修改 pytest、未跳过测试、未改变权限。已核对全仓 1890 项收集结果及单入口夹具可见性。

2026-11-01 12:13（Asia/Shanghai）新 CLI 已真实读取 `https://example.com/`，标题 Example Domain、实体 577 字节、SHA-256 `25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484`；TLS SNI/证书验证保持开启。该一次后端验证不证明搜索或模型效果，不作为日常联网自动测试。

## 验收

`python -m pytest -q tests/features/test_web_reading.py tests/test_web_policy.py tests/test_architecture.py`：73 项通过。故障注入只在 DNS 边界模拟解析失败/重绑定，其余连接、重定向、正文、超时和取消均为真实本地 ThreadingHTTPServer，不使用 MockTransport。没有迁移或删除已有场景。

Ruff format/lint、Pyright 与全量 pytest 是每项提交门禁。下文的真实百科后端验证不证明通用互联网覆盖或模型效果；C01.3 工程闭环见下文，C01.4 真实模型效果保持待办。


## 独立百科搜索（C01.2）

```powershell
python -m adam.web.search "Nelson Mandela" --backend wikipedia --limit 5
```

这是首个无需密钥的真实后端，范围仅为 English Wikipedia。选型原因：本机可直接访问官方 [MediaWiki Search API](https://www.mediawiki.org/wiki/API:Search)，而此前 DuckDuckGo HTML 可用性探测返回 202 挑战；不绕过挑战、不把不可用后端或百科范围声称为通用互联网检索。C01.2 先提供独立服务/CLI，C01.3 已接入原发现/权限/日志入口（见下文）；C01.4 的五题真实对照已另行执行，严格完整通过 2/5、另 1 项部分通过，见文末；本项后端验证不代替该质量记录。

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


## 搜索—阅读—片段引用（C01.3）

可信配置按需添加：

```toml
[web]
enabled = true
search_backend = "wikipedia"
search_max_results = 5
```

原字节、正文、重定向及期限配置仍适用。默认整个 web 关闭；只有 `enabled=true` 注册 `read_webpage` 和 `read_web_source`，`search_backend="wikipedia"` 另注册 `search_web`；省略搜索后端不隐式联网或注册搜索，只有后端配置但 enabled=false 也不启用。后端不能由模型参数或网页选择，不接受私网/代理/凭据配置；搜索查询会发送给百科，勿输入密钥/私密资料。普通聊天不创建网络资源。

1. 用原 `discover_tools` 按需发现三个工具，后续请求才取得可执行名单；同批发现不立即授予权限。
2. `search_web(query=...)` 返回 `status=candidates/no_results`、独立 `search` 及 `output_limited`。返回的 `search_snippets` 只帮助选择下一次 URL，不能据摘要声称已读正文；完整候选按结果预算减条，URL/摘要不局部裁断。原本有候选却一条也装不下时明确 `output_limit`，不伪装无答案。HTTP/API 失败保留分类，不重试。
3. `read_webpage(url=...)` 通过原网络策略抓取，先将正文缩短到可交付范围，再生成 `source_id=web-<SHA-256>` 和 `excerpt`。ID 规范化摘要绑定完整 source 元数据（请求/最终 URL、UTC 时间、实体版本、大小、正文、截断及重定向等）及实际保存前缀；不是当前网页版本、全球数据库主键或授权凭据。`source.sha256` 标识原实体，`excerpt.text_sha256` 标识交付文本，二者用途不同。
4. 回答可使用实际 `excerpt.citation`，格式为 `web-<id>:start-end`；偏移按提取后正文 Unicode 字符，从 0 开始，end 不含尾字符，不是 HTML 字节/屏幕位置。例如保存正文里的数字位于 12～14 时引用相应两字符范围。
5. `read_web_source(source_id=..., start=12, count=2)` 只回查所属当前会话已保存的 read_webpage 结果，返回逐字 `excerpt.text`、范围/摘要/citation、完整来源元数据、`saved_chars` 和 `next_offset`。count 为 1～4000，按实际 JSON 转义后大小二分保留最长可交付非空前缀，输出缩短后下一偏移紧接实际返回字符；`next_offset=null` 只表示已保存部分读完。来源 truncated=true 时未保存尾部始终未知，不能据此宣称读完网页。

来源不另写数据库或附件，不依赖 artifacts 开关。现有 ToolInvocation 在正文结果交付前保存完整 `tool_finished`，WebSources 通过已验证日志中的原 turn/request/tool_call 关联读取，只接受原 `read_webpage` 的结果。来源日志失败停止本轮；即使网络已完成，没有已保存结果也不交付回查凭据、不重抓或伪造成功。原成功工具结果可以作为失败轮次留下的真实观察被回查，不因此宣称整轮成功。旧的无 source_id 工具记录仍能读取/重放，但不能事后补造新引用，需用户明确重新读取网页。

每次回查重新核对 source_id、正文/元数据/范围与片段摘要，并在日志 IO 返回后复查活动会话；缺失、损坏、错绑、范围越界和跨会话分别失败，日志 IO 错误传播而不伪装为缺少来源。内容/版本哈希用于一致性检查，不是数字签名；不声称能防御攻击者改写全部可信宿主日志。网站后来变化不会改写已保存快照，新抓取获得新 ID。

主子均通过原 ToolRegistry/ToolExecutor。无状态搜索/抓取可只读并发，各自在线程创建并关闭连接；来源回查访问 SessionStore，声明串行只读。主子只注入各自持锁 store，不把父日志授予子、也不允许父按子 ID 读取其私有记录；委派结果须保留归属。所有工具仍受发现、模式、预算、Hook、取消与原日志规则约束：网页中的“已授权”不授予越界读或改写，act 编辑仍需用户确认，拒绝立即停止本轮。录制重放消费冻结工具事实，零搜索/抓取/来源文件回查。

### 本项工程验收

[17 项业务](../tests/features/test_web_research.py)、[28 项引用/范围边界](../tests/test_web_evidence.py) 与 [6 项新增配置边界](../tests/test_web_config.py) 覆盖真实本地百科搜索/重定向站点、摘要与正文冲突、同 URL 新旧快照、准确数字引用、重启回查、截断尾部、主子隔离、plan/act 注入与原批准、空结果/限流/预算/Hook、真实在途取消、结果日志保存失败、普通聊天零网络及离线重放。协议故障仅在已验证日志读取端口模拟错绑/损坏/所有者变化/IO 故障；不能代替真实会话持久化测试。原网页测试不迁移、不删除场景。

本项网页/搜索/来源/配置/架构联合回归 246 项通过，全仓 Ruff format/lint 与 Pyright 通过。测试中的模型响应由 MockTransport 固定，搜索与网页为真实本地 HTTP，文件/主子工作线程/JSONL/取消及重启实际运行。这证明工程闭环，不证明真实模型能正确搜索或引用；最终回答未新增语义拦截器，固定真实 5 题、引用支持、成本和耗时已由文末 C01.4 对照单列，不能把上述工程回归当成模型质量。无付费模型请求、无截图或看图。全仓 Ruff format/lint、Pyright、全量 pytest 作为本项提交门禁。

## 固定五题真实对照（C01.4）

2026-11-02 13:28～13:29（Asia/Shanghai），固定题目、评分条件和测试页后，分别关闭、开启 WebConfig 运行同一组五题。使用 [Qwen 官方 Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF/tree/bc640142c66e1fdd12af0bd68f40445458f3869b) 的 Q4_K_M 权重，固定 revision `bc640142c66e1fdd12af0bd68f40445458f3869b`，模型 SHA-256 `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`；llama.cpp b12032、原生工具模板、温度 0、seed 42、关闭思考和提示缓存。实际服务参数及模板参数保存在 [完整对照记录](evaluations/c01-web-comparison.json)。现有 Qwen2.5-VL 的原生模板不编码工具定义，独立协议烟测未返回工具请求；该结果保留，未混入五题计分。

真实对照在 B16 rebase 前执行：HEAD 为 `0479201`，暂存的图片保护修复随后提交为 `fbdb8f7`；B16 同步后单独执行工程回归，未把它描述成新的真实模型对照。运行时摘要、执行草稿及 JSONL/模型正文摘要均已核对。

所有回答经过正式 `build_application`、默认关键词/按需工具发现、`LLMClient` SSE、ContextBuilder/RequestRunner、工具执行器及 JSONL。每题每组独立临时会话和空工作目录；关闭记忆、规则、反思、规划和自动摘要，使用 plan 模式，8 次模型请求/12 次工具调用上限。不读取默认模型配置或真实密钥，不重试失败批次、不根据回答改题。两组仅改变网页能力开关；应用生成的临时路径及可用工具目录仍会进入模型上下文，不属于盲测。

| 固定题目 | 关闭联网 | 开启联网 | 引用与结论核对 |
| --- | --- | --- | --- |
| 时效：当前联合国秘书长及首次就任日 | 未查证，回答只承诺搜索 | 7 次真实百科查询后达到 8 次模型请求上限，整轮失败 | 没有读取正文、最终答案或支持引用；不能算答对 |
| 冲突：同一指标两版发布记录 | 错把 URL 当附件，未读资料 | 在百科搜索受控项目，未读取给定的两页 | 未建立 24→28、修订与日期的依据，失败 |
| 无答案：固定名称的登记日期 | 没有搜索能力，未验证 | 真实百科查询返回空结果，没有虚构日期，末段限定搜索范围 | 通过；前文“百科没有信息”的措辞较宽，不能据此推出百科或全网不存在 |
| 重定向：测试公告的验证码和规范 URL | 未读公告 | 实际读取 `/latest`，HTTP 302 后到 `/releases/r7`，回答 TERN-482 | 唯一 1 条页引用可回查，范围有效且正文支持验证码和测试资料说明；通过 |
| 无需搜索：幂等定义及数学例子 | 定义、恒等函数例子正确，零工具 | 同样正确，零工具 | 两组均分成两句话，未遵守题面的“一句话”，严格判部分通过 |

严格完整通过为关闭联网 **0/5**、开启联网 **2/5**；两组各有 1 项部分通过。只看核心语义分别为 1/5、3/5。引用质量逐条人工检查，不把合法 ID/字符范围当成语义支持，也不把工具请求/轮次成功当成答题成功。当前证据说明本地小模型可以在部分题目正确选工具，不能声称时效搜索或冲突处理可靠。

真实搜索仅为 English Wikipedia；时效和无答案题实际访问官方 API，冲突与重定向使用固定本地 HTTP 测试页，只证明这两个受控资料任务。联合国官网独立核验页返回 HTTP 202，未采用其正文；另行抓取公开百科 `Secretary-General_of_the_United_Nations` 的冻结快照，明确列出 António Guterres、2017-01-01，供独立评审，不注入被测模型。原页 URL、抓取时间、实体摘要和逐字核验片段均在记录中；不据此宣称独立主源核验成功。

| 实际消费 | 关闭联网 | 开启联网 |
| --- | --- | --- |
| 物理模型请求 | 12 | 20 |
| 服务端报告输入 / 输出 token | 15846 / 994 | 49466 / 1069 |
| 五题累计实际墙钟耗时 | 22.233 秒 | 40.506 秒 |
| 费用 / 币种 | 未知 / 未配置 | 未知 / 未配置 |

耗时包括单题主流程和记录保存，不包含权重下载、服务启动或独立来源核验；五题样本不构成性能基准。32 次请求的 token 用量均已报告。无付费模型调用；未设置单价，本机电力/硬件成本未计量，不能把未知费用写成零。

复现入口为 [scripts/web_evaluation.py](../scripts/web_evaluation.py)：先 `python -m scripts.web_evaluation prepare .cache/new-web-batch`，再显式启动兼容工具的本地模型并 `python -m scripts.web_evaluation run .cache/new-web-batch --manifest .cache/local-text/manifest.json`。只接受 `http://127.0.0.1:<port>/v1` 模型端点；不覆盖旧批次。宿主仅为受控阅读页放行该批精确环回源，`open_search` 及生产网络默认策略保持原样。提交版将已执行草稿的配置与单题编排提取为辅助函数，并修正离线报告的工具名关联；原草稿摘要、原始 JSONL/模型正文摘要和完整回答保留，不用重跑替换结果。

[评估器回归](../tests/features/test_web_evaluation.py) 的 9 项用例覆盖实际本地重定向与来源保存、两组零网络普通问题、已知/未知用量、模型 HTTP 失败不重试、旧批次及改题拒绝、仅网页开关的组间差异、错范围/错来源及“范围有效但结论错误”。只在模型 HTTP 边界注入 SSE/503；网页、主应用、权限、记录和资源关闭实际运行。未知用量遵循现有停止契约，以 None 保留，不伪造成功或零消费。相关联合回归 182 项通过，新增评估器最终 9 项再次通过；收集 3320 项，原 3281 场景无遗漏，新增为 B16 的 30 项及本项 9 项。全仓 Ruff 与 Pyright（含新增脚本）通过；全量 pytest 为本项提交门禁。注释审查已逐项核对新增函数的契约，没有删除或改写既有 Python 设计说明；架构新增独立评估入口图，生产职责保持。真实质量结论由上述实际对照记录支持。
