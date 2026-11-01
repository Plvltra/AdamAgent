# DOM 浏览器观察与动作（C02.1 / C02.2）

最近核对：2026-11-01。已实现单标签观察、引用时效与四类逐次批准动作；会话标签/弹窗/下载管理及完整任务闭环仍为 C02.3～4，不因本项具备基本 close 就提前完成。

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
- 每次观察生成新 `observation_id` 并替换元素引用表，上一观察的全部引用失效。浏览器与标签身份在本次资源生命周期内不变；导航重新创建文档身份。一次资源关闭后不持久恢复这些引用。
- 引用包含浏览器、标签、观察和随机控件 ID，不是 CSS、屏幕坐标或宿主代码。`inspect_reference` 验证当前观察、原节点身份、DOM revision、连接/可见/禁用状态、页面文字及控件属性状态。DOM 替换、导航、同名替代、属性/文本更新，以及不触发 MutationObserver 的目标输入 `.value` 改变都要求重新观察；不按选择器偷偷匹配替代控件。
- 检查结果只是此刻证据，不是未来动作的授权或原子保证。C02.2 执行时重新验证，走原 ToolExecutor 预检与逐次批准；只提供固定动作，不注册任意 evaluate 模型工具。
- 返回主文档 `innerText`、原生按钮/链接/输入/选择/文本框、明确 button/link role 及 contenteditable 控件的摘要。过滤隐藏、inert、禁用和 aria-disabled 祖先；名称采用 aria-label、原生 label、控件文字或 placeholder，不声称是完整无障碍树。暂不遍历 iframe / Shadow DOM / Canvas 控件。
- 默认最多 16000 个 JS UTF-16 文字单元、100 个控件，分别标记截断，不切开代理对，非法代理单元以替代字符规范化；标题及名称最多 300，role/type 最多 100。URL 上限 8192 字符；Python 配置接受正整数和有限耗时。字段不导出输入值、密码、cookies 或 localStorage，但页面自己渲染的敏感文字仍可能进入可见正文；使用临时测试站点而不是日常账户。

`BrowserError.kind` 区分 `backend_missing`、`network_denied`、`stale_reference`、`http_error`、`redirect_limit`、`timeout`、`browser_failed`、`closed` 和 `cleanup_failed`。超时/取消/SDK 故障后禁用原后端，不隐式重放请求。清理失败不覆盖正在传播的原始错误，而附加清理说明；正常退出的清理失败会明确抛错。

## 网络与安全边界

未执行获批动作时只允许 GET/HEAD，阻止页面自动 POST；WebSocket 全部拒绝、Service Worker 禁用、下载不接受，不授予麦克风/相机等权限。URL 拒绝凭据、控制字符、反斜杠和非 HTTP(S) 协议。浏览器不使用宿主代理。

Playwright 路由只处理重定向链首请求。因此适配器 `route.fetch(max_redirects=0, max_retries=0)` 先检查 3xx；显式打开的主页面由 `navigate` 在同一期限内逐跳重新打开，最多六个请求并拒绝循环。不能将 3xx 直接 fulfill 给 Chromium 自动跟随。子资源及非显式打开期间的重定向保守拒绝，即使目标 origin 已允许。临时响应在处理后 dispose，避免轮询响应驻留到上下文结束。

这里的 origin 策略**不是操作系统网络沙箱**，不提供 `web` 抓取器的 DNS 固定、公共地址校验或硬字节/进程内存限制。白名单站点可执行 JS，GET 也可能产生服务端副作用；浏览器自身流量、DNS、WebRTC 等不因此获得完整系统隔离。不得把它标成原只读工具或用来浏览未经信任的内网/真实账户；跨站资源未获宿主授权时页面可能不完整。DOM 观察脚本也不是对恶意网页的防篡改边界：网页文字和控件资料始终不可信，绝不据此授予权限。

`timeout_seconds` 限制每次启动、导航、观察及引用检查；并非整个浏览器生命周期总时限。取消轮询约 50ms，实际在途协程停止后关闭资源。上下文/SDK 关闭各有五秒期限，浏览器进程关闭为三十秒（实测已安装 Chrome 正常退出可达约十六秒），不宣称取消返回为零延迟或浏览器是硬资源沙箱。

## 真实验收与限制

业务入口：[test_browser_observation.py](../tests/features/test_browser_observation.py)；配置、无后端导入及失败边界：[test_browser_policy.py](../tests/test_browser_policy.py)；依赖方向：[test_architecture.py](../tests/test_architecture.py)。全量测试环境需安装 `browser` extra 和 Chrome；缺少后端不以 skip/xfail 宣称已验收。

测试启动真实本地 ThreadingHTTPServer 和无头 Chrome：动态 JS 查询结果、隐藏/禁用控件、版本身份、同名替代与纯属性变化、重新观察、跨资源/伪造引用、有界输出、正常导航、跨站及多跳重定向、子资源重定向/POST/WebSocket 零请求、独立 cookies、503、不重发、在途取消、超时禁用、创建/清理故障及独立 CLI。远端模型请求为零，未产生截图或查看图片。

2026-11-01 实测 Playwright 1.64.1、Chrome **156.0.8078.74**；Python 与浏览器依赖安装在 Adam2 独立 `.cache/c-venv`，没有改动指向主工作区的 `.venv` 联接。定向与全量结果随本项提交记录；真实模型选控件、真实公开网站适配及 C02.3～4 未验收。

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
```

可信配置必须给出确切 origin，不接受路径、凭据或任意站点开关。这里只应用显式浏览器站点权限，不把 additional_read_dirs 当成网络授权；所有浏览器工具声明 EXTERNAL_ACTION、保持串行，在 plan 模式不可用。按需发现、工具/请求/token 预算、项目规则和前置 Hook 仍先于浏览器处理器；子任务暂不注册浏览器工具，不继承主会话资源或交互审批。

- `browser_open(url)`：检查站点，再请求宿主批准确切 URL；批准后才延迟创建当前主会话独有的临时上下文，返回 opened 与观察。没有审批回调默认拒绝。普通聊天不创建浏览器；观察/动作也不隐式创建。
- `browser_observe()`：重新观察当前会话已有页面；不点击、不输入，替换旧引用。已有页面的异步 JS 可继续产生获准 GET，请勿将它声明为通用无副作用只读工具。
- `browser_action(browser_id, tab_id, observation_id, element_id, action, value?, scroll_y?)`：仅 click / fill / select / scroll；输入/选择值最多 4000 字符，滚动垂直增量为绝对值不超过 2000 的非零整数。fill 只支持原生 input/textarea，select 只支持原生 select；密码、文件控件、脚本、CSS 选择器和额外参数在批准前拒绝。

服务先校验原节点并生成不可变 BrowserApproval（URL、控件名、完整引用、确切输入/增量），CLI 将整份计划 JSON 转义后展示；只有明确 y 批准，EOF 拒绝，Ctrl+C 取消。网页标签不是可信指令，值也不是密码安全通道，不在此工具输入凭据。批准返回后重新检查取消/宿主会话、观察与当前原节点；等待期间同名替换不会转而操作新控件。后端使用固定程序取得原 ElementHandle 并执行 SDK 原生动作，不接受模型 JavaScript；网页持续变化的时间竞态不是跨页面事务或系统级安全保证。

获批动作调用期间允许白名单内 POST（包括页面的相关事件处理），不宣称批准了一次 HTTP 或能限制网站只发一条请求。主页面 301/302/303 结果逐跳按 GET 打开并校验；307/308 保守拒绝，绝不自动重提表单。WebSocket、Service Worker 与跨站请求限制不因批准放宽；GET 同样可能有副作用。

动作返回 `dispatched` 只说明一次控件动作已派发；业务成功必须读取真实结果页。本次动作后旧引用全部失效，即使模型重复提交旧引用也不再派发。新观察失败保留 dispatched 和 observation_unavailable，不能要求重做。SDK/OS/参数异常发生在派发入口后时按 BrowserActionUnknownError / execution_unknown 停止本轮，取消保持原中断；不能把可能已产生的副作用转换为可重试 ToolError。工具开始日志失败不进入处理器；结果日志保存失败停止，不请求下一次模型或自动重发。

结果限长在完整 JSON 内缩短正文/控件列表，保留引用结构和派发状态；连元数据都超限时只报告观察不可用，不返回损坏引用。主应用通过 ExitStack 拥有每会话资源；关闭应用回收全部已用上下文，取消/未知结果立即丢弃当前资源。当前没有标签页、弹窗/下载产物、会话关闭回调与崩溃恢复管理，因此不勾选 C02.3。

### 动作验收

业务测试：[test_browser_actions.py](../tests/features/test_browser_actions.py)；协议、配置和 CLI 边界：[test_browser_actions.py](../tests/test_browser_actions.py)。真实 Chrome 填写/选择/滚动/点击后，本地服务器独立记录一次 POST 与字段，303 结果页作为成功证据；工具/请求/轮次结果进入实际 JSONL，重启读取相同事件。其余场景覆盖发现/模式/Hook/预算拒绝、默认拒绝停止批次、敏感/额外参数、批准期间 DOM 替换、跨会话与旧引用/重复提交、取消清理、开始/结果日志失败、真实 POST 后 SDK/OS 结果丢失、观察失败及重定向策略。

MockTransport 只模拟模型的协议输出，不证明真实模型能选择控件；所有网页提交均为本机临时服务器，零付费模型请求，零截图。本节不替代 C02.4 固定任务与真实模型质量验收。