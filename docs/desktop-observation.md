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

动作后重新观察并保存新证据；dispatched 仅证明调用派发，不证明保存成功。原生调用或后续观察异常成为 EXECUTION_UNKNOWN，不能自动重试；输出超限不截断控件树，已保存媒体可能保留。共享输入租约、锁屏和持续停止在 C03.3，不提前宣称已实现。

8 项工具闭环验证覆盖 fill/坐标click/固定key、批准来源与动作后观察、拒批/plan/批准期间变化、未知结果停止、旧版本重复拒绝；与架构依赖共37项通过。观察模块原4项已通过，未重复全量。模拟仅替换桌面端口和远端模型传输，真实 Windows 动作另留实机证据。


C03.2 实机状态（2026-11-02）：尚未通过。正式工具循环在第一个观察时拒绝非前台目标，0 次批准、0 次派发；独立 UIA ValuePattern 仍读到原文本。视觉代理确认实际截图为壁纸而非测试窗。可见启动、自身激活和前台返回值排查后仍未恢复；同会话存在 LogonUI，可能与锁屏/桌面隔离相关，但不把进程存在当作完整原因证明。两个自建测试进程已关闭，未盲重试任何派发。非图像证据见 [工具拒绝](evaluations/c032/blocked-tool-result.json)、[独立读回](evaluations/c032/blocked-readback.json)。真实 fill/key/click 待可交互桌面恢复后补验，C03.2 暂不勾选完成。
