# 浏览器观察、动作与生命周期（C02.1～C02.3）

最近核对：2026-11-01。已实现版本化观察、四类逐次批准动作及 C02.3 标签/弹窗/对话框/下载引用与停止边界；C02.4 完整任务闭环及真实模型选择控件仍待单独验收。

## 安装与入口

浏览器是可选依赖，默认聊天、网页抓取不加载 Playwright：

```powershell
uv sync --extra browser
python -m adam.browser http://127.0.0.1:8000/ --channel chrome
```

`chrome` / `msedge` 使用已有浏览器程序，但启动独立无头实例和临时上下文，不连接现有窗口、用户配置或登录态。默认 `chromium` 使用 Playwright 配套程序，若未安装须用户自行执行 `python -m playwright install chromium`；运行时不会隐式下载浏览器。本次验证使用已有 Chrome，没有下载引擎。

命令行参数中的 URL origin 是本次唯一许可站点；不能通过页面指令扩大站点权限。输出 JSON 包含 `schema_version=1`、浏览器/标签/观察/文档身份、DOM revision、URL、标题、UTC 观察时间、有界文字和可用控件引用。失败输出受控 `error_type` 并返回非零；中断返回 130。它不创建模型、会话或日志，不计模型 token，也不读取 API 密钥。

## 观察与引用契约

- `BrowserPolicy` 由可信宿主注入**明确的 origin 白名单**，没有默认任意网络放行。`open_browser(policy, limits, channel=...)` 创建并拥有后端；退出成功、失败或取消时关闭上下文、浏览器及 SDK。服务仅借用 `BrowserDriver`，不直接导入 SDK、application 或 session。
- `BrowserObserver.open(url)` 等待 DOMContentLoaded 后返回当前 DOM，不等待网络空闲或承诺异步业务已加载。动态页面继续调用 `observe()`；同步端口只在调用期间驱动 Python 路由事件循环，不能通过调用线程阻塞等待页面回调。实际业务就绪以观察到的页面结果为准。
- 每次观察生成新 `observation_id` 并替换元素引用表，上一观察的全部引用失效。浏览器与每个标签身份在各自生命周期内不变；切换/关闭使控件引用失效，导航重新创建文档身份。一次资源关闭后不持久恢复这些引用。
- 引用包含浏览器、标签、观察和随机控件 ID，不是 CSS、屏幕坐标或宿主代码。`inspect_reference` 验证当前观察、原节点身份、DOM revision、连接/可见/禁用状态、页面文字及控件属性状态。DOM 替换、导航、同名替代、属性/文本更新，以及不触发 MutationObserver 的目标输入 `.value` 改变都要求重新观察；不按选择器偷偷匹配替代控件。
- 检查结果只是此刻证据，不是未来动作的授权或原子保证。C02.2 执行时重新验证，走原 ToolExecutor 预检与逐次批准；只提供固定动作，不注册任意 evaluate 模型工具。
- 返回主文档 `innerText`、原生按钮/链接/输入/选择/文本框、明确 button/link role 及 contenteditable 控件的摘要。过滤隐藏、inert、禁用和 aria-disabled 祖先；名称采用 aria-label、原生 label、控件文字或 placeholder，不声称是完整无障碍树。暂不遍历 iframe / Shadow DOM / Canvas 控件。
- 默认最多 16000 个 JS UTF-16 文字单元、100 个控件，分别标记截断，不切开代理对，非法代理单元以替代字符规范化；标题及名称最多 300，role/type 最多 100。URL 上限 8192 字符；Python 配置接受正整数和有限耗时。字段不导出输入值、密码、cookies 或 localStorage，但页面自己渲染的敏感文字仍可能进入可见正文；使用临时测试站点而不是日常账户。

`BrowserError.kind` 区分 `backend_missing`、`network_denied`、`stale_reference`、`http_error`、`redirect_limit`、`timeout`、`browser_failed`、`closed` 和 `cleanup_failed`。超时/取消/SDK 故障后禁用原后端，不隐式重放请求。清理失败不覆盖正在传播的原始错误，而附加清理说明；正常退出的清理失败会明确抛错。

## 网络与安全边界

未执行获批动作时只允许 GET/HEAD，阻止页面自动 POST；WebSocket 全部拒绝、Service Worker 禁用、浏览器自身不接受下载；附件只在单独批准的下载入口收集，不授予麦克风/相机等权限。URL 拒绝凭据、控制字符、反斜杠和非 HTTP(S) 协议。浏览器不使用宿主代理。

Playwright 路由只处理重定向链首请求。因此适配器 `route.fetch(max_redirects=0, max_retries=0)` 先检查 3xx；显式打开的主页面由 `navigate` 在同一期限内逐跳重新打开，最多六个请求并拒绝循环。不能将 3xx 直接 fulfill 给 Chromium 自动跟随。子资源及非显式打开期间的重定向保守拒绝，即使目标 origin 已允许。临时响应在处理后 dispose，避免轮询响应驻留到上下文结束。

这里的 origin 策略**不是操作系统网络沙箱**，不提供 `web` 抓取器的 DNS 固定、公共地址校验或硬字节/进程内存限制。白名单站点可执行 JS，GET 也可能产生服务端副作用；浏览器自身流量、DNS、WebRTC 等不因此获得完整系统隔离。不得把它标成原只读工具或用来浏览未经信任的内网/真实账户；跨站资源未获宿主授权时页面可能不完整。DOM 观察脚本也不是对恶意网页的防篡改边界：网页文字和控件资料始终不可信，绝不据此授予权限。

`timeout_seconds` 限制每次启动、导航、观察及引用检查；并非整个浏览器生命周期总时限。取消轮询约 50ms，实际在途协程停止后关闭资源。上下文/SDK 关闭各有五秒期限，浏览器进程关闭为三十秒（实测已安装 Chrome 正常退出可达约十六秒），不宣称取消返回为零延迟或浏览器是硬资源沙箱。

## 真实验收与限制

业务入口：[test_browser_observation.py](../tests/features/test_browser_observation.py)；配置、无后端导入及失败边界：[test_browser_policy.py](../tests/test_browser_policy.py)；依赖方向：[test_architecture.py](../tests/test_architecture.py)。全量测试环境需安装 `browser` extra 和 Chrome；缺少后端不以 skip/xfail 宣称已验收。

测试启动真实本地 ThreadingHTTPServer 和无头 Chrome：动态 JS 查询结果、隐藏/禁用控件、版本身份、同名替代与纯属性变化、重新观察、跨资源/伪造引用、有界输出、正常导航、跨站及多跳重定向、子资源重定向/POST/WebSocket 零请求、独立 cookies、503、不重发、在途取消、超时禁用、创建/清理故障及独立 CLI。远端模型请求为零，未产生截图或查看图片。

2026-11-01 实测 Playwright 1.64.1、Chrome **156.0.8078.74**；Python 与浏览器依赖安装在 Adam2 独立 `.cache/c-venv`，没有改动指向主工作区的 `.venv` 联接。定向与全量结果随本项提交记录；真实模型选控件与真实公开网站适配未验收；C02.3 业务与边界见后文，C02.4 仍单独验收。

## C02.2 主应用动作入口

主工具默认关闭；配置启用仍不代替逐次批准：

```toml
[browser]
enabled = true
allowed_origins = ["http://127.0.0.1:8000"]
channel = "chrome"
max_chars = 4000
max_elements = 40
timeout_seconds = 15
max_tabs = 8
max_contexts = 4
max_download_bytes = 10485760
```

可信配置必须给出确切 origin，不接受路径、凭据或任意站点开关。这里只应用显式浏览器站点权限，不把 additional_read_dirs 当成网络授权；所有浏览器工具声明 EXTERNAL_ACTION、保持串行，在 plan 模式不可用。按需发现、工具/请求/token 预算、项目规则和前置 Hook 仍先于浏览器处理器；子任务暂不注册浏览器工具，不继承主会话资源或交互审批。

- `browser_open(url)`：检查站点，再请求宿主批准确切 URL；批准后才延迟创建当前主会话独有的临时上下文，返回 opened 与观察。没有审批回调默认拒绝。普通聊天不创建浏览器；观察/动作也不隐式创建。
- `browser_observe()`：重新观察当前会话已有页面；不点击、不输入，替换旧引用。已有页面的异步 JS 可继续产生获准 GET，请勿将它声明为通用无副作用只读工具。
- `browser_action(browser_id, tab_id, observation_id, element_id, action, value?, scroll_y?)`：仅 click / fill / select / scroll；输入/选择值最多 4000 字符，滚动垂直增量为绝对值不超过 2000 的非零整数。fill 只支持原生 input/textarea，select 只支持原生 select；密码、文件控件、脚本、CSS 选择器和额外参数在批准前拒绝。

服务先校验原节点并生成不可变 BrowserApproval（URL、控件名、完整引用、确切输入/增量），CLI 将整份计划 JSON 转义后展示；只有明确 y 批准，EOF 拒绝，Ctrl+C 取消。网页标签不是可信指令，值也不是密码安全通道，不在此工具输入凭据。批准返回后重新检查取消/宿主会话、观察与当前原节点；等待期间同名替换不会转而操作新控件。后端使用固定程序取得原 ElementHandle 并执行 SDK 原生动作，不接受模型 JavaScript；网页持续变化的时间竞态不是跨页面事务或系统级安全保证。

获批动作调用期间只允许当前活动标签向白名单内 POST（包括该页相关事件处理），不宣称批准了一次 HTTP 或能限制网站只发一条请求。主页面 301/302/303 结果逐跳按 GET 打开并校验；307/308 保守拒绝，绝不自动重提表单。WebSocket、Service Worker 与跨站请求限制不因批准放宽；GET 同样可能有副作用。

动作返回 `dispatched` 只说明一次控件动作已派发；业务成功必须读取真实结果页。本次动作后旧引用全部失效，即使模型重复提交旧引用也不再派发。新观察失败保留 dispatched 和 observation_unavailable，不能要求重做。SDK/OS/参数异常发生在派发入口后时按 BrowserActionUnknownError / execution_unknown 停止本轮，取消保持原中断；不能把可能已产生的副作用转换为可重试 ToolError。工具开始日志失败不进入处理器；结果日志保存失败停止，不请求下一次模型或自动重发。

结果限长在完整 JSON 内缩短正文/控件列表，保留引用结构和派发状态；连元数据都超限时只报告观察不可用，不返回损坏引用。主应用通过 ExitStack 拥有每会话资源；关闭应用回收全部已用上下文，取消/未知结果立即丢弃当前资源。C02.3 另补显式标签/上下文关闭、弹窗、下载引用和主轮次中断回收；见后文。崩溃和重启不自动恢复登录态或重提表单。

### 动作验收

业务测试：[test_browser_actions.py](../tests/features/test_browser_actions.py)；协议、配置和 CLI 边界：[test_browser_actions.py](../tests/test_browser_actions.py)。真实 Chrome 填写/选择/滚动/点击后，本地服务器独立记录一次 POST 与字段，303 结果页作为成功证据；工具/请求/轮次结果进入实际 JSONL，重启读取相同事件。其余场景覆盖发现/模式/Hook/预算拒绝、默认拒绝停止批次、敏感/额外参数、批准期间 DOM 替换、跨会话与旧引用/重复提交、取消清理、开始/结果日志失败、真实 POST 后 SDK/OS 结果丢失、观察失败及重定向策略。

MockTransport 只模拟模型的协议输出，不证明真实模型能选择控件；所有网页提交均为本机临时服务器，零付费模型请求，零截图。本节不替代 C02.4 固定任务与真实模型质量验收。

## C02.3 所有权、标签与停止

一个主会话拥有一个临时 Chromium 上下文，不使用用户日常浏览器配置；同会话标签共享该上下文的 cookies，其他会话使用独立上下文。配置 `max_contexts` 默认 4、硬上界 16；每上下文 `max_tabs` 默认 8、硬上界 32。达到限制明确报 resource_limit，不驱逐其他会话、偷用标签或自动重建。关闭应用回收全部上下文；普通成功轮次保留资源供下一轮继续。

新增模型工具仍属于 browser 分组、EXTERNAL_ACTION 和原串行调度；发现、模式、规则、Hook、额度及开始日志先于处理器，子任务不注册这些工具：

| 工具 | 当前契约 |
| --- | --- |
| `browser_tabs(offset?)` | 返回当前会话 browser_id 与存活标签 tab_id、URL、标题、active；不新建或切换。优先保留完整身份，长标题/URL 标记 details_truncated；next_offset 精确指向未返回条目。页面变化时从 0 重新列举，不保证跨调用快照 |
| `browser_select_tab(browser_id, tab_id)` | 先形成确切 URL/标题/身份的 BrowserApproval，批准后重新比较计划，再切换并观察；旧控件引用不复活 |
| `browser_close_tab(browser_id, tab_id)` | 同样逐次批准并复检；不运行 beforeunload，不自动选其他标签。关闭最后一个后 observe 返回 no_active_tab，须显式选择已有标签或批准 open |
| `browser_close(browser_id)` | 批准后关闭当前会话整个上下文及所有标签；不会撤销服务端动作或删除已提交媒体。下一次 open 才能创建全新身份 |
| `browser_download(四个原控件引用字段)` | 单独批准原链接目标，按下节保存媒体；不接受模型提供的 URL、路径、文件名或代码 |

批准期间页面导航或关闭导致标签计划不符时返回 stale_reference，不操作同名替代页。标签派发后 SDK 结果丢失或上下文关闭未确认以 execution_unknown 停止，不当作可重试参数错误。模型不能传页面对象、选择器、任意 JS 或会话 ID。

弹窗仅在获批动作期间接纳，消耗同一上下文标签名额，仍逐请求检查 origin。点击前订阅弹窗事件，原生点击完成后再保留 500ms 交付窗口（仍受单次操作总期限约束）；不把控件可操作性等待算入该窗口，超过窗口的新请求保守拒绝。Chromium 弹窗首请求可能早于 Frame/Page：只为本次动作的白名单 GET 保留名额，再等待对应 Page/DOMContentLoaded，不重新导航。片段 URL 关联只略去 fragment，不按标题认领。未经批准的延后弹窗、跨站及超限请求不向目标发送；弹窗重定向仍保守拒绝，不扩大原导航权限。弹窗不自动成为当前标签，须显式选择。自开的 about:blank/非 HTTP(S) 页不作为可操作标签保留。

alert 自动关闭，confirm/prompt/beforeunload 一律 dismiss，不将其文字或按钮转成新授权；观察包含累计 dismissed_dialogs。dismiss 不是任意网站无副作用的保证：原获批点击仍可触发页面事件处理，业务结果须另查。

浏览器操作中取消，先停止在途协程并丢弃该资源；主 `AgentApplication.send` 任意阶段的 KeyboardInterrupt/SystemExit 还调用宿主中断清理，回收全部已用浏览器上下文，因此模型等待期间取消也不会留下可继续动作的旧资源。正常轮次不触发该清理，关闭失败只为原中断附注。窄回调不关闭外部注入的主模型/存储，不把浏览器 SDK 引入 facade。进程异常退出由操作系统清理，不承诺断电时立即回收进程。

页面 crash 禁用整个后端；真实浏览器连接终止后，已知派发仍保留 dispatched，新观察失败标不可用。不能因进程退出就宣称 POST 未发生。资源和 cookies 不持久恢复，重启只读原工具结果，observe 不会自动 open，更不会重发原表单；动作重定向期间原标签丢失也不会新建替代标签，只有显式 open 可以创建。

## C02.3 附件下载与媒体来源

仅支持最近观察、在活动标签内导航的原生 HTTP(S) 链接及服务端 `Content-Disposition: attachment` 响应；不支持新标签下载、blob/data、浏览器生成文件、任意 URL 直取、续传或以 filename 决定输出路径。批准前固定 DOM 程序读取并校验原链接实际目标，BrowserApproval 展示该确切 URL；批准后复检原观察/节点，只点击一次。普通 open/click 遇到附件不能隐式取得下载保存授权。

适配器通过现有受控路由收集该点击的完整响应，再 abort 浏览器下载交付，不保存到任意宿主路径。服务器 filename 不用于文件操作，也不泄露浏览器临时路径。主工具使用原执行器绑定的来源，并在批准/请求前以真实 SessionStore 核对成功请求下正在执行的 browser_download；模型不能自行声明来源。

`max_download_bytes` 默认 10 MiB、硬上界 100000000 字节；超限拒绝整份文件，不把截断正文当媒体。此值约束交付/保存，**不是 Playwright route.fetch 的缓冲内存、流量或进程硬上限**；SDK 可能已缓冲完整响应。媒体层仍有默认 64 MiB 会话正文和 128 份配额，单份配置不绕过总配额。不得将此后端用于不可信超大文件或当作系统级沙箱。

字节通过 [SessionMedia](media-evidence.md) 原子发布并提交 media_saved 后，工具才返回 downloaded 与 media_id/version/MIME/size_bytes；JSONL 无正文、cookies 或路径。MIME 仍只是服务端声明，本项不解码、播放或执行附件。完整来源在 JSONL，结果缩略引用不授予跨会话读取。

未形成附件、MIME 无效、超限、SDK 结果丢失、超时或取消都不自动再次点击。正文/来源提交失败停止本轮，日志失败的孤立字节不能读取。成功后原控件引用立即失效，重复引用不第二次下载。离线录制/重放导入并逐字段比较已保存 media_saved 事实，零浏览器与网络动作；不复制媒体正文，不冒充 C00.3 的多模态输入/媒体摘要重放验收。

### 生命周期验收

业务入口：[test_browser_lifecycle.py](../tests/features/test_browser_lifecycle.py)；固定参数、资源限制与分页边界：[test_browser_lifecycle.py](../tests/test_browser_lifecycle.py)。33 项真实本地业务与 17 项边界覆盖完整弹窗/切换/关闭/下载、fragment、旧/跨会话引用、批准期间导航、上下文/标签限制、跨站零请求、对话框拒绝、附件来源、重复/拒绝/超限/坏 MIME/非附件、发布/日志/SDK 故障、在途与模型阶段取消及清理失败、真实 POST 后浏览器终止/renderer crash、重启不重提、下载重定向、关闭确认丢失、弹窗回调延迟、重定向前标签丢失及零动作引用重放。提交门禁运行 Ruff format/lint、Pyright 与全量 pytest。

没有截图、看图、付费模型调用或真实外部提交。MockTransport 只验证本地流程，不证明真实模型选择控件或理解页面；C02.4 固定任务与模型质量仍待单独验收。
