# C03.1 真实窗口观察验收

时间：2026-11-02。仅创建并操作独立合成 WinForms 窗口，没有调用生产桌面动作接口。

最终真实运行通过，视觉子代理已直接查看 initial.png、moved.png、restored.png：
- 三张图均显示完整 Adam C03 Test 窗口、Adam C03 observation acceptance 标签、Only synthetic test data 12345 文本框；没有大面积遮挡。矩形边缘包含 Windows 窗口阴影/边框透明区域，可见少量背景，截图不是纯客户端区域。
- initial: rect [200,180,800,540], 600x360, DPI 96；截图点 [300,180] 映射屏幕物理点 [500,360]。
- moved: rect [350,270,1070,690], 720x420, DPI 96；截图点 [360,210] 映射屏幕物理点 [710,480]。
- 切换到第二个自建窗口后，原绑定观察失败：Target is not authorized visible foreground window。
- 恢复原窗口后观察成功；handle/process/runtime identity 与首次保持一致。
- 控件树包含 Window、Text、Edit 和窗口非客户端控件。Edit 的 UIA Name 由关联标签提供，不等于文本框 Value；截图可见真实文本框内容，当前模型未读取 ValuePattern。
- 实际仅验证本机 96 DPI。96/120/144/192 的 screen_point 模型参数检查通过，不代表实际多显示器或跨DPI测试通过。未改变系统DPI。

之前一次隐藏启动后，通过前台检查却截到背景3D/代码内容，保留 failed-hidden-initial.png。该失败证明 IsWindowVisible 和 foreground 检查不是无覆盖/内容属于目标的保证；最终切换为可见窗口启动后解决。本记录不把失败隐藏。
另一次测试启动等待过短，读取陈旧句柄导致 COMError；等待窗口 identity 文件更新并确认启动日志后重试成功。

证据：results.json、initial.json/png、moved.json/png、restored.json/png；每帧 JSON 包含窗口身份、rect、dpi、width、height、controls、SHA256 和映射。
测试脚本 verify.py 与 window.ps1 保留用于复现。脚本 finally 关闭两个自建窗口。没有改动生产代码。

补充：本次移动并 resize，没有执行 WinForms Scale(1.25)；实际控件缩放仍未验证。最终测试进程45564及早期明确记录的测试进程39608/39852/47400/6320已退出。PID44932仍存在，但其命令行不匹配测试脚本，未再次停止，避免误操作其他进程。

