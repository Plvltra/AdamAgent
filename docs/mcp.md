# MCP：工具、资源、提示模板与传输

Adam 是客户端：发现工具、发送参数、读取结果。服务端负责实现查询并约束路径。
tools 是可调用操作；resources 是 URI 标识的资料；prompts 是可复用提示模板。E7 映射 tools，B15.1 已接入固定 URI 的文本 resources，B15.2 已接入有来源的文本 prompts；B15.3.2 又加入二进制引用与未读取的资源链接。
MCP 负责互通协议，不替代 Agent 的循环、额度、记录或批准，也不是操作系统沙箱。

依赖使用官方 Python SDK 2.3.0（uv.lock 固定），不是自行实现 JSON-RPC。
安装或更新依赖运行 uv sync --frozen。默认 [mcp] enabled=false，不启动进程。
E7.2 已接入工具目录。把示例配置中的整个 [mcp] 表替换为以下配置，并将程序/脚本路径改成实际位置：

~~~toml
[mcp]
enabled = true

[[mcp.servers]]
alias = "files"
command = 'G:/Myself/Adam/.venv/Scripts/python.exe'
args = ['G:/Myself/Adam/examples/mcp_readonly.py', '--workspace', '.']
tools = ["read_project_text"]
timeout_seconds = 10
~~~

服务工作目录是 Adam 的工作目录。模型看到 mcp_files_read_project_text；
参数仍只含 path，不能修改程序、工作根或允许列表。
示例服务只读根内的短 UTF-8 文本，拒绝越界、链接逃逸、凭据和超大文件。
服务器只读声明只是筛选条件；配置启用表示用户选择了可信程序，不是操作系统沙箱。
本版只给主 Agent 注册 MCP；子任务继续使用既有只读工具和 Skill，避免额外服务进程扩散。

Schema 使用 Draft 2020-12 校验嵌套对象、数组、联合类型等；只允许文档内部引用，
不会为解析 Schema 隐式访问外部 URL。启动/映射失败通过 ExitStack 回收已启动服务。
正常退出先关闭子任务，再关闭 MCP；模型或主 SessionStore 仍按原有生命周期关闭。

同一个应用复用一个服务连接；启动失败、断开或超时不自动重发。
取消后关闭连接并回收进程；服务端取消只是协议通知，客户端停止等待并不证明远端工作已停止，
因此本地资源验收实际检查了进程退出。
stdout 只供协议使用，服务日志写 stderr。客户端不把 LLM 密钥传给服务。

参考：[官方 Client](https://py.sdk.modelcontextprotocol.io/client/)、
[stdio 传输](https://py.sdk.modelcontextprotocol.io/client/transports/)、
[工具与只读声明](https://py.sdk.modelcontextprotocol.io/servers/tools/)、
[ClientSession](https://py.sdk.modelcontextprotocol.io/api/mcp/client/session/)。
调用使用 session.call_tool，避免高层 Client 在 HEADER_MISMATCH 后自动重发；
工具 is_error 必须转成失败，不能把正常收到协议响应等同查询成功。

## 验收与成本

工程验收运行：
~~~powershell
python -m pytest -q tests/features/test_mcp_connection.py tests/features/test_mcp_tools.py tests/features/test_mcp_failures.py
~~~

使用真实 SDK 服务子进程制造工具错误、主动退出、长等待和在途取消；确认进程退出，调用日志仅一次。
Schema 嵌套类型在客户端校验；服务另检查路径和凭据。名称冲突或非只读工具导致启动失败，
已经打开的服务由组装资源栈关闭。服务声明只读不能验证其实现，用户应配置可信程序。

从项目根运行可重现的只读性能对照：
~~~powershell
python -m scripts.compare_mcp --workspace . --path skills/verify-project/skill.toml --runs 3
~~~

输出冷启动、直接函数均值、连接复用均值、结果 JSON 字符数及内容一致性。
2026-10-29 在本机 Python 3.11.9 / MCP SDK 2.3.0 测量上述命令：冷启动 1.243 秒；
直接调用均值 1.46 毫秒，复用连接调用均值 29.27 毫秒；结果 JSON 分别为 196 / 477 字符，内容一致。
这不调用模型，token/模型费用为零；耗时与机器和缓存状态相关，不把一次测量当性能保证。
协议多出进程、序列化与传输开销；收益是可以独立替换服务实现，不需要修改 Agent 的工具循环。
简单本地函数仍适合直接调用，只有跨进程或独立扩展有价值时才使用 MCP。


## B15.1 资源目录与读取

资源是服务按 URI 提供的资料，不是 Adam 自行打开的本机路径或 URL。协议允许应用决定如何将资源用于上下文；Adam 将显式允许的 URI 映射为两个只读工具入口，仍走原发现、参数校验、额度、Hook、日志和重放。依据：[MCP resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)、[官方 SDK Client](https://py.sdk.modelcontextprotocol.io/client/)、[资源服务示例](https://py.sdk.modelcontextprotocol.io/servers/resources/)。实现使用 uv.lock 中的 SDK 2.3.0，不自行实现协议或旧版订阅。

可把上方服务配置中的 tools 改为空数组，并增加 resources；也可同时允许两类能力：

```toml
[mcp]
enabled = true

[[mcp.servers]]
alias = "files"
command = 'G:/Myself/Adam/.venv/Scripts/python.exe'
args = ['G:/Myself/Adam/examples/mcp_readonly.py', '--workspace', '.']
tools = []
resources = ["project://readme"]
timeout_seconds = 10
```

[教学服务](../examples/mcp_readonly.py) 将 project://readme 映射到工作目录的短 README.md，复用原服务的路径和大小限制（最多 16 KiB、3000 字符）。练习时使用含短 README 的临时项目。配置不自行创建资料、扩展权限或启动服务；只有 enabled=true 且实际打开应用才启动。仅配置 resources 的服务不发送 tools/list。URI 列表最多 16 项，必须是完整、互不重复且每项不超过 2048 字符的固定 URI；不展开模板或匹配通配符。

| 模型入口（alias=files） | 行为 |
| --- | --- |
| mcp_files_list_resources | 刷新允许范围内的目录，返回 server、uri、name、title、description、mime_type 与声明大小 |
| mcp_files_read_resource | 按 uri 读取文本；可传 expected_version，变化时拒绝返回新正文 |

按需工具模式下先发现这些入口，随后请求使用。目录最多四页、64 项；重复游标、重复 URI、缺少已配置资源或元信息超长会失败，不返回残缺目录。读取只接受所请求 URI 的 1～8 项文本，总正文 UTF-8 不超过 64 KiB；服务声明大小已超限时在请求前拒绝，实际响应仍重新检查。二进制、跨 URI 拼接和不完整结果拒绝。边界是在 SDK 接收响应后检查，不是整个服务或宿主的内存硬上限。

成功结果保留 server、原 uri、mime_type、文本和 size_bytes。version 是宿主对规范化的 URI/类型/文本列表计算的 SHA-256，version_source 明确为 content_sha256；它不是服务端发布版本，也不证明资料正确。expected_version 只保证这次实际读取匹配指定内容，不能保证来源此后不变。正文和资源描述不提升为系统指令；较长结果仍遵循原输出/附件规则。

低层 ClientSession 读取避免高层缓存及自动再次请求。目录页请求共享总等待窗口，读取超时、断开、格式错误、版本不符或取消后关闭并回收连接，不自动重试。模型不能选择服务程序、修改 URI 允许列表或借 URI 直接访问宿主。本阶段仍只向主 Agent 注册 MCP，原子任务范围保持。B15.3.1 已提供下方显式 HTTP 传输，非文本引用留在 B15.3.2。

[26 项业务场景](../tests/features/test_mcp_resources.py) 使用真实 SDK stdio 服务、文件、分页与取消；模型 HTTP 使用 MockTransport。覆盖目录过滤、版本变化、字节/条数限额、未授权 URI、无效配置、损坏分页、二进制拒绝、超时/断开无重试、真实进程回收、教学示例及完整聊天/离线重放。与原 MCP 工具、架构及重放共 72 项通过；教学文件的 Windows 换行预期已修正并单独复验。Ruff、Pyright 和链接检查完成，主线提交门禁运行全量 pytest；没有调用付费模型。


## B15.2 提示模板目录与取得

MCP prompts 是可复用的消息模板，参数为字符串映射，协议可返回多条 user/assistant 消息。官方客户端示例通常由用户选择模板；Adam 本阶段选择将已配置模板封装成只读资料获取工具，方便原工具循环按需查询。这个封装是 Adam 的设计，不是 MCP 要求模型自行执行模板。依据：[官方提示模板](https://py.sdk.modelcontextprotocol.io/servers/prompts/)、[官方客户端](https://py.sdk.modelcontextprotocol.io/client/)。

在既有 [[mcp.servers]] 中添加下面一行；教学服务提供 review_text，必填 text、可选 focus（默认 correctness）。也可只保留 prompts 并省略 tools/resources；默认仍不启动服务。

```toml
prompts = ["review_text"]
```

配置最多16个不同模板名，名称为1～40字符的字母开头、字母/数字/下划线/连字符。每个服务至少显式允许一种能力。仅模板服务不会发送 tools/list 或 resources/list。URI、工具与模板三份允许列表相互独立，模型无法扩展它们。

| 模型入口（alias=files） | 行为 |
| --- | --- |
| mcp_files_list_prompts | 刷新允许模板的名称、标题、说明、参数名及 required 声明；不预取正文 |
| mcp_files_get_prompt | 传 name 与 arguments 字符串对象，返回服务来源、模板名、文本消息、内容摘要和 reference_only 标记 |

例如 name="review_text"、arguments={"text":"def add(a, b): return a + b", "focus":"correctness"}。目录最多四页、64项；每个模板最多16个参数，参数名与模板名同限。名称重复、已允许项缺失、坏游标、超长说明和无效参数声明均失败。获取前校验当前目录：必填参数不可缺，未知参数拒绝，每值最多2000字符；空文本允许，不擅自补齐服务器默认值。

只接收1～16条文本消息，总正文 UTF-8 不超过64 KiB。图片、音频、嵌入资料和不完整结果在本阶段拒绝；不会为模板内的链接发起读取。返回的角色标签只是工具结果 JSON 中的数据，不插入真实系统/用户/助手消息，也不替用户批准操作。普通输出限额和附件机制继续生效。version 是当次规范化角色/文本列表的 SHA-256，不是服务版本，也不证明模板安全或正确。

目录刷新和获取共用原连接、总等待、取消与进程回收；低层 ClientSession 每次只发送对应请求，不自动重发或跟进 input_required。参数错误在请求服务前返回，可修正后继续；协议、内容、超时、断开或在途取消导致连接关闭。边界检查在 SDK 接收响应后进行，不能声称限制了恶意服务的总内存。只配置可信本地程序；MCP 不是 OS 沙箱。

[31 项业务场景](../tests/features/test_mcp_prompts.py) 使用真实 SDK stdio 服务及进程检查，覆盖目录/参数、来源与内容摘要、外部角色拒绝和隔离、超限、取消/断开无重试、名称冲突、默认关闭、教学服务及模型工具循环/零服务重放。与资源、原 MCP 工具、重放和架构检查共103项通过（92.94秒）。首轮录制夹具输入不一致已修正，整组重新通过；模型仅为 MockTransport，没有付费请求。Ruff、Pyright、链接和注释审查分别核对；主线提交门禁执行全量 pytest。


## B15.3.1 显式 HTTP 传输与认证

同一 MCPConnection 可连接本地 stdio 或用户配置的 Streamable HTTP；工具/资源/模板仍走原目录、参数、额度、日志和重放。使用 SDK 2.3.0 的 streamable_http_client 与 httpx2，未自行实现协议。依据：[官方传输说明](https://py.sdk.modelcontextprotocol.io/client/transports/)。

将服务的 command/args 替换为 url，token_env 可选，仅填环境变量名。以下域名是占位示例，须替换为你选择的真实服务及其工具名；程序不会自动寻找或连接该地址。

```toml
[mcp]
enabled = true

[[mcp.servers]]
alias = "remote"
url = "https://mcp.example.com/mcp"
token_env = "ADAM_MCP_TOKEN"
tools = ["ping"]
timeout_seconds = 10
```

配置端点必须是 HTTPS；为本地练习，仅数值回环 127.0.0.1 或 ::1 可以使用 HTTP，不将 localhost 或一般内网地址自动视为例外。端点不得包含用户名、密码、查询参数、片段、空白或控制字符。模型不能更改端点、程序或认证。URL 与 command/args 二选一；默认 enabled=false 时不连接网络，也不读取认证环境变量。

HTTP 客户端验证服务器证书，不提供关闭校验的用户配置，不继承环境代理。每次发送核对规范化后的精确端点，并在收到重定向时拒绝；不仅设置 follow_redirects=False，因为 SDK 有自己的重定向处理。TLS 不受信或握手失败时不降级明文、不连接其他端点。

token_env 的值仅在连接期间读入 Authorization: Bearer 头，不写回配置、不加入模型资料、会话 JSONL 或录制。未设置、空值、非 ASCII 或非法 Bearer 格式会在 HTTP 请求前失败；错误不回显值。认证不从模型参数或通用 headers 表传入。本阶段未提供 OAuth、自动刷新凭据或多账号管理。测试使用虚构令牌，不读取真实账户密钥。

一次业务调用只发起对应的 POST；超时、HTTP 错误、协议错误或取消后关闭连接，不自动重发工具动作。SDK 的后台 GET/SSE 可能按其协议重连，受等待窗口约束；这与重发业务 POST 不同。HTTP 关闭不能证明远端任务已停止，也不会杀死远程服务；本地 stdio 服务仍按原方式回收进程。SSE 单事件采用 SDK 的1 MiB上限，JSON 响应仍在接收后做业务检查，不把它称为总内存硬限制。

本机测试服务位于 [mcp_http_service.py](../tests/fixtures/mcp_http_service.py)。它在随机回环端口启动真正的 SDK 服务，提供 ping、docs://hello 和 review；由测试拥有并清理进程。普通模式返回 JSON，sse 模式返回流。错误、认证和 TLS 模式仅供验收，不用于生产部署。资源的可选空说明已兼容 SDK 的合法输出，必填名称和 URI 仍不可为空。

验收：远程及原 MCP/架构/重放先合计131项通过（103.74秒）；增加真实自签名 TLS 拒绝及环境代理隔离后，29项远程场景重新全部通过（22.01秒）。共覆盖132个不同场景。实际经过 TCP、SDK 和真实子进程；仅 LLM HTTP 使用 MockTransport。认证材料在捕获的模型请求、会话、录制和本地日志中均未出现；离线重放不连接服务。没有调用付费模型或外部 MCP 服务。

httpx2、uvicorn、cryptography 原已在锁文件中，本次分别声明生产/测试直接依赖，未更新包版本或下载包。默认 uv 缓存不可访问，使用工作区缓存执行 uv lock --check --offline，71包锁定一致。后续仍由主线提交钩子执行静态检查与全量 pytest。


## B15.3.2.1 二进制引用存储基础

[MCPMediaStore](../adam/mcp/media_store.py) 是 B15.3.2.1 建立的宿主 API。该基础步骤独立验收；B15.3.2.2 已在整个协议结果验证后调用此存储，把实际引用放回原工具结果，接入说明见下节。

prepare_media 接收 base64、声明 MIME 和宿主提供的 MediaSource，先限制编码长度，再严格解码，最多1 MiB，不截断。结果 PreparedMedia 暂持原始字节及引用，准备阶段不创建目录。来源含服务别名、服务身份摘要、tool/resource/prompt、名称或 URI、结果位置；由连接侧提供，不由模型自由指定，也不替代外层工具执行日志。

MediaReference 只包含格式 mcp_binary_v1、media_id、内容 SHA-256、实际字节数、声明 MIME 和来源。media_id 同时绑定内容、大小、类型与来源，所以同一来源重复返回相同内容得到同一引用，换服务或类型不会混用引用。引用不含 base64、路径或认证材料。MIME 只是服务声明，不据此认定文件格式有效或声称模型看懂了图片。

MCPMediaStore(root) 由宿主指定专用目录，默认总量16 MiB、最多128项，构造不创建目录。save_many 接收1～16个 PreparedMedia，验证整批摘要并在持锁后预检配额；重复内容只占一份。正文以固定 ID.bin 原子发布，写入前 fsync；引用只有成功返回后才能由调用方加入日志。额度不足不驱逐旧引用，不返回半份文件。发布中途 IO 故障不返回部分成功，可能留下尚未被日志引用的缓存。

read 接收完整引用，限制文件路径、核对完整大小和 SHA-256，返回真实字节；缺失、损坏、路径伪造和来源字段篡改均失败，不重新调用服务。读取供持有记录的宿主使用，未注册为模型工具；本阶段不提供媒体解码、执行或多模态模型请求。

异常退出残留的 .pending-* 不提升为正式内容，并计入总量和数量，避免反复中断绕过配额。缓存不自动清空或恢复，清理前由宿主确认哪些引用仍需保留。要在重启后读取，调用者须同时保留引用记录与目录；只有记录而无二进制文件不能恢复原内容。

[23项业务场景](../tests/features/test_mcp_media_storage.py) 实际保存和读取二进制字节，覆盖重启、来源绑定、重复引用、严格格式、编码与字节上限、空内容、整批额度、残留、损坏、fsync/替换故障和两个独立进程竞争。连同架构检查共45项通过（2.00秒）。首次大参数直接作为 pytest ID 导致 Windows 临时路径过长，改为稳定短 ID 后重新通过；未改动生产限制。Ruff、Pyright、链接及注释审查单独核对，主线提交门禁运行全量 pytest。


## B15.3.2.2 非文本结果接入

聊天组装现在把 sessions_dir/_mcp_media 的 MCPMediaStore 注入主 MCPConnection。[ResultContent](../adam/mcp/content.py) 将工具、资源和提示中的 image、audio、嵌入二进制转换为实际可读引用。原始 base64 不放入对应结果字段、会话或模型上下文，模型只收到内容摘要、字节数、声明 MIME 和来源；content_interpreted=false 明确表示尚未解释媒体。

来源由连接配置和真实调用提供：服务别名、程序/参数或 HTTP 地址的身份摘要、tool/resource/prompt、实际工具名/URI/模板名及结果位置。身份摘要不使用认证变量的值。结果仍走原工具额度、实际执行日志、会话保存和离线重放，来源字段不替代外层 turn/request/tool 关联。

resource_link 只保存有界 URI 和声明元信息，并标记 content_loaded=false；read_allowed 仅说明 URI 是否在配置允许列表中，不表示已经读取或目录仍有效。返回链接不触发网络、宿主路径或额外资源读取。嵌入文本仍是工具资料，提示角色仍是参考字段，均不提升为真实系统/用户指令。

工具和提示最多16项，资源最多8项；普通文本与嵌入文本共用64 KiB上限，结构化工具结果另限64 KiB。每份二进制最多1 MiB，存储沿用16 MiB/128项总配额。size_bytes 统计实际收到的正文，不把尚未读取的链接声明大小算作已接收字节。SDK 已接收并构造对象后才校验这些限制，因此不把它们称为传输层内存硬上限。

转换器先验证完整结果；资源还须匹配 expected_version，之后才发布字节。后半部分损坏、文本合计超限、URI不一致或版本变化不会提前发布前半份有效媒体。存储失败返回工具失败，不返回成功引用；IO中断仍可能留下未引用缓存，沿用基础层的失败语义。服务返回 is_error 时保留失败标记，即使它同时附带可读诊断附件。

普通文件工具、项目规则、Skill、文档源、审查入口、独立知识入口及子任务均把 _mcp_media 作为保留目录。宿主可用 MCPMediaStore.read(完整引用) 校验并取回字节；未增加模型可读任意媒体路径或多模态模型请求。这里是工具路径保护，已获批准的宿主命令和用户配置的外部服务仍须自行约束，不能据此宣称操作系统隔离。子任务仍不注册 MCP。

[23项接入场景](../tests/features/test_mcp_media_results.py) 使用真实 SDK stdio 服务、真实文件和模拟 LLM HTTP，覆盖三类结果、角色/链接、整份响应拒绝、版本/保存故障、主子目录保护、会话重开和零服务重放。与媒体存储、原 MCP/HTTP/取消和架构检查共167项通过（138.68秒）；Ruff、Pyright通过。测试字节仅验证传输与保存，不宣称图片或音频格式可解码。B15.3.3 将补跨传输混合场景，不把本小步的 stdio 验收等同全部远程媒体已验收；主线提交门禁执行全量 pytest。
