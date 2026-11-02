# Windows 桌面观察

C03.1 提供 `adam.desktop.adapters.windows.WindowsDesktop(handle, process_id)` 与 `DesktopObserver(backend, media.publish)`，调用方显式选定独立测试窗口并传入真实 MediaSource。安装可选依赖 `uv sync --extra desktop`。C03.1 是宿主观察 API；C03.2 通过显式组装接入普通聊天工具，见下节。不会枚举或自动激活用户日常窗口。

适配器使用 [pywinauto UI Automation](https://github.com/pywinauto/pywinauto) 获取树及截图，默认最多 200 个控件、1600 万像素。截图与树保留窗口句柄、进程、UIA runtime ID、物理屏幕矩形、DPI、标题和节点父索引。内容字符串有界，不把窗口标题或屏幕文字视为授权。截图经原 SessionMedia 发布，Observation 的完整版本由身份、几何、树和 PNG 共同计算。

线程临时使用 per-monitor DPI context，结束后恢复；`screen_point` 按截图像素到物理矩形映射，允许负屏幕坐标，不再重复乘 DPI。观察前后目标须为同一可见前台窗口；切换、移动或身份变化时整次拒绝，不自动重试，不返回部分树。两个时点检查不是原子快照，不能证明内容未变化或无覆盖层。UIA 原生调用只能在阶段边界协作取消，不承诺硬中断。桌面租约与持续停止逻辑在 C03.3。

自动验证只替换窗口观察端口，不触碰真实桌面：真实媒体存取、来源、几何变化版本，以及 96/144/192 DPI 参数化映射、越界和取消，共 4 项。参数化 DPI 不冒充多显示器真实测试。


真实验收（2026-11-02）：独立视觉代理实际查看自建 WinForms 窗口的初始、移动/resize、恢复三张截图，标题、标签和文本框完整。600×360 / rect [200,180,800,540] 变为 720×420 / [350,270,1070,690]，中心点分别映射 [500,360]、[710,480]。切换第二自建窗后拒绝原窗口，恢复后身份不变。实际 DPI 为 96，未更改系统设置。UIA Edit.Name 是关联标签，并非 ValuePattern 内容，本项没有声称读取文本框实际值。

首次隐藏启动曾得到背景截图，原始失败保留在本地缓存而不加入仓库；可见启动后复验正确，说明前台/可见性检查不能证明无遮挡。完整元数据见 [窗口证据](evaluations/c031/window-observation.json)，文字核图报告见 [视觉审查](evaluations/c031/window-review.md)。原三张截图在 `.cache/c031-desktop/`；仓库只保留非图像报告，避免混入背景内容。

本项新增注释已核对，保留 DPI、非原子观察、取消和来源边界。同步 main ddc298c 后的配置/日志/主应用组装及隔离命令、OCR 相关 66 项通过，Ruff/Pyright 通过；没有运行全量测试。


独立第二批实际控件缩放通过：测试窗执行 WinForms Scale(1.25) 并同步字体，标签从 [238,241,434,258] 变为 [246,249,491,269]，文本框从 [238,286,628,307] 变为 [246,305,732,330]；两控件截图中心分别映射到 [368,259]、[489,317]，与 UIA 物理矩形中心一致。独立视觉代理已查看前后图确认放大且未裁切。DPI 始终 96，本项证明应用内缩放而非系统跨 DPI；见 [缩放证据](evaluations/c031/scaled-window.json)，原图在 `.cache/c031-scale/`。


## C03.2 有界动作

宿主调用 `build_application(..., desktop_backend=WindowsDesktop(handle, pid), confirm_desktop=callback)` 后，原工具目录出现 `desktop_observe` 与 `desktop_action`；默认没有桌面工具，子任务不继承目标或批准回调。CLI 暂不自动选择桌面目标，显式宿主绑定避免从模型参数获得任意窗口权限。

观察输出包含窗口、控件树、截图媒体和唯一 version；每次新观察均使旧动作引用失效，即使像素相同也不能重复。动作使用 `operation=click/fill/key`，click 优先 UIA Invoke，控件不支持时在原控件点击；fill 只接受有界字面文本，不把 `{ENTER}` 等作为键盘指令；无控件 click 使用截图像素点，转换为本次物理屏幕坐标。快捷键只允许 TAB、SHIFT+TAB、ENTER、ESC、CTRL+A，不接收任意键盘脚本。

原执行器处理发现、固定请求名单、模式、Hook、额度和真实来源；动作在 plan 中拒绝。宿主收到不可变 DesktopApproval，缺失回调或拒绝即停止本轮。批准后复检完整帧（包括 PNG）和当前会话，原生派发前再核对窗口、目标控件身份和位置。像素闪烁也可能使完整帧不同，此时拒绝并要求新观察，不放宽成盲点。

动作后重新观察并保存新证据；dispatched 仅证明调用派发，不证明保存成功。原生调用或后续观察异常成为 EXECUTION_UNKNOWN，不能自动重试；输出超限不截断控件树，已保存媒体可能保留。共享输入租约、锁屏和停止逻辑已在 C03.3 实现，真实恢复闭环状态见下节。

8 项工具闭环验证覆盖 fill/坐标click/固定key、批准来源与动作后观察、拒批/plan/批准期间变化、未知结果停止、旧版本重复拒绝；与架构依赖共37项通过。观察模块原4项已通过，未重复全量。模拟仅替换桌面端口和远端模型传输，真实 Windows 动作另留实机证据。


C03.2 实机状态（2026-11-02）：尚未通过。正式工具循环在第一个观察时拒绝非前台目标，0 次批准、0 次派发；独立 UIA ValuePattern 仍读到原文本。视觉代理确认实际截图为壁纸而非测试窗。可见启动、自身激活和前台返回值排查后仍未恢复；同会话存在 LogonUI，可能与锁屏/桌面隔离相关，但不把进程存在当作完整原因证明。两个自建测试进程已关闭，未盲重试任何派发。非图像证据见 [工具拒绝](evaluations/c032/blocked-tool-result.json)、[独立读回](evaluations/c032/blocked-readback.json)。真实 fill/key/click 待可交互桌面恢复后补验，C03.2 暂不勾选完成。


## C03.3 输入租约与停止

`DesktopLease` 使用当前用户临时目录内同一文件锁，跨线程/进程串行保护一次观察或动作（包含批准、派发与收尾）。竞争立即拒绝，不排队执行可能过时的动作；异常和进程退出释放系统锁，锁文件残留不代表占用。它约束遵守此租约的 Adam 调用，不能阻止用户或其他程序输入。

Windows10+ 先查询 WTSInfoEx，必须明确活动且解锁，再查询线程桌面的 UOI_IO；任何查询失败或未知均拒绝。只读取状态前缀，原生缓冲区总会释放，不落盘用户名。实测说明单独 UOI_IO 不够：它返回 true 时前台仍为 LockApp；加入会话锁状态后明确拒绝。原查询与修正后的真实只读证据分别见 [输入资格](evaluations/c033-input-desktop.json)、[锁屏拒绝](evaluations/c033-session-lock.json)。实现依据微软 [WTSINFOEX_LEVEL1](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w) 和 [UOI_IO](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw) 契约。

成功观察启动每50ms只读看守；检测到失焦、锁屏或查询故障后锁存失效，即使焦点回来也不能沿用旧引用。动作复检不清除失效；只有显式重新观察才能恢复。每次实际观察/派发仍同步检查当前窗口和桌面。50ms轮询可能漏掉更短暂切换，不承诺系统级原子输入或硬中断正在进行的原生调用；但检测到失效、取消或失败后不派发后续动作。

工具异常、拒批和取消都会丢弃旧引用；主轮次在工具之间中断时也清理桌面看守，应用退出再次关闭是幂等的。看守未能在期限内退出时拒绝恢复，不悄悄启动替代线程。恢复必须重新观察，没有自动重试。

新增10项验证通过：真实子进程租约竞争/异常释放、取消/窗口变化/租约冲突后旧引用拒绝与重新观察、看守失效锁存、工具之间中断清理，以及WTS活动/解锁/未知状态和内存释放。此前桌面相关联合17项通过，Ruff/Pyright通过，未跑全量。最新实机批次仍在首个观察前被前台条件拒绝，0批准/0派发，两个输出文件均不存在，见 [本批记录](evaluations/c033-window-attempt.json)。实际输入/保存/失焦恢复闭环仍待解锁后验证；C03.2/3保持实机待验状态，C03.4未完成。


### C03.2 解锁后实机完成

2026-11-02 用户确认解锁后，独立视觉代理通过真实 build_application → 原执行器 → DesktopTools → WindowsDesktop 完成 fill → TAB → 控件 click 保存，3次批准、3次 dispatched，动作均产生新观察。仅模型请求使用 MockTransport；Windows UIA、键盘、控件调用和文件保存均真实执行。独立 UIA ValuePattern 与测试应用保存文件均为 `C032 literal {ENTER}`，说明输入保持字面量。代理核图确认窗口完整可见、文本正确、保存按钮获焦点。

生产代码及完整帧复检未修改；测试窗初始焦点设为按钮。之前两轮分别因帧变化和 Codex 前台遮挡拒绝，没有成功派发或保存；失败仍保留缓存。本批自建窗口/后台均已退出。坐标 click 已有自动测试，本批没有实机验证；C03.3 的失焦/取消恢复及 C03.4 故障闭环仍待验，不由本次成功替代。

证据：[结果与视觉文字报告](evaluations/c032/unlocked-report.json)、[三次批准](evaluations/c032/unlocked-approvals.json)、[独立读回](evaluations/c032/unlocked-readback.json)。保存文件和原图保留 `.cache/c032-unlocked-final/`，主代理未读取图片。C03.2 现勾选完成，前文未通过状态为历史记录。本次仅同步验收文档，无 Python 改动，不重复运行已有测试。

### C03.3 解锁后恢复验收完成

2026-11-02 用户再次确认解锁后，真实 DesktopTools 与 WindowsDesktop 在独立测试窗口完成失焦、取消及 close 生命周期恢复。次窗口遮挡并夺走焦点后，动作在批准后复检拒绝，未保存；返回主窗口仍拒绝旧观察，显式观察后才恢复保存。已取消令牌拒绝动作且保存时间不变，旧观察不能复用，重新观察后恢复。close 停止看守并使旧引用失效，重新观察后可继续。

本批共4次批准、3次实际派发；独立 UIA ValuePattern 与文件均为 Only synthetic test data 12345，恢复后保存时间更新。独立视觉代理核图确认遮挡及最终测试窗口完整可见。自建窗口已退出、看守已停止。批准由已授权脚本回调确认，没有测试人工批准界面；close 指工具生命周期，不是销毁重建窗口；本次未主动锁屏，锁屏拒绝沿用此前真实证据。

证据：[逐步记录](evaluations/c033/unlocked-report.json)、[独立读回](evaluations/c033/unlocked-readback.json)。原图、脚本及真实会话保留于 .cache/c033-unlocked-retry/。C03.3 完成，前文待验文字为历史状态；C03.4 综合验收仍单独待办。本次仅归档实机证据与文档，无 Python 改动，不重复运行 pytest；注释无需变更，架构已核对无结构变化。
