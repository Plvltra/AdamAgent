# C10.4 Android 实机边界与策略对照

日期：2026-11-03。隔离 Android API30 模拟器 emulator-5556，独立 ADB 5041，仅 com.adam.practice。生产代码未修改；脚本和下述相对缓存证据位于 G:\Myself\Adam2\.cache\c104-android，未运行 pytest。

## 原工具链实机边界

每项通过 build_application、LLMClient、SessionStore、原工具注册和执行器；确定性边界只替换远端 HTTP 响应，ADB 未替换。CountObserver 只透明计数生产输入。

- 前台观察 → Home：旧 mobile_action 被 `practice application is not foreground` 拒绝，0 审批、0 派发；回到前台后观察成功，720×1280；初始版本再次使用被 stale 拒绝。
- 旋转：旧 mobile_action 被 `device or page changed; observe again` 拒绝，0 审批、0 派发；横屏稳定后新观察成功，1280×720；初始版本再次使用被 stale 拒绝。
- 批准回调取消共享 CancellationToken：1 次批准、0 派发，抛 RequestInterrupted。另一个独立取消验收在后续轮次重用取消前引用，仍被 stale 拒绝，1 次批准、0 派发。四项见 report.json/boundaries 及各目录 events.json/http.json。
- 收尾确认 user_rotation=0，竖屏恢复。边界各自耗时未单独计时。

## 单样本策略对照

| 策略 | 成功/样本 | 实际工具/动作 | 证据 |
| --- | --- | --- | --- |
| C10.2 固定脚本（MockTransport 决策） | 1/1 | 7 次输入动作、保存 1 次 | ../c102/report.json，随机名称 C102-88ba46d015，备注 Note-8fbb9defd1，run-as 独立读回匹配 |
| C10.4 本地 Qwen3-4B-Q4_K_M 自选策略 | 0/1 | discover mobile + observe；0 mobile_action、0 审批、0 派发、0 保存 | model/request-*.json、response-*.json、events.json、http.json |

真实模型按原应用 on_demand 工具发现、普通工具及 act 模式运行。CPU 4 线程、ctx16384、parallel1、reasoning off、temp0、seed42、no-cache-prompt；请求未注入或改写参数。预算请求18、工具18、输出500、会话100000。提示只提供目标随机文本、发现/观察和版本/停止约束，没有操作路线。

模型发现 mobile、成功观察后，第三次 HTTP 以最终文本结束，仅称将继续操作，没有实际派发。没有人为追加“继续”或重试刷成功。随机名称 C104-eb351cbe1a、备注 Note-0bbabd0d8e。run-as 读取 files/record.json 返回 code1，No such file or directory，故判失败。3 次 HTTP，5728 prompt tokens + 242 completion tokens = 5970 total tokens；端到端99.5秒。使用本地 CPU，无付费API；电耗未计。

原来源链：session 3030f049；turn 5c19ee043de60841099fff4aaea67a80；mobile_observe request dc891dd410a71c3fa80549d508766bab；tool_call P9QgHXUD8rgfo1Kz4nGDVuovAHb4mwrV；media_saved 与 tool_started/tool_finished 均保存在 model/events.json。

## 视觉与限制

已独立看图 final.png：720×1280 竖屏测试表单，名称/备注均空，显示 No saved record，与独立文件读回一致。截图 SHA256：ad309a047550468b7e8113a91b73e59175b70d2b445b1a179c1f0f2893578370。

两策略各只有一个样本、随机文本不同，使用同一应用同类任务。固定脚本和小模型的单次结果不能推断总体成功率或泛化能力。该模型未进入动作阶段，不能据此宣称其逐次审批/多步保存能力已验证；审批和取消边界由独立确定性实机用例验证。生产工具截图为媒体引用加UI层级；该文本模型没有直接视觉输入。

资源清理结果见 android-cleanup.json；保留 AVD 和证据。
