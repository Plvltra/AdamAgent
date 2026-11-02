# B20 修改后诊断

B20.1 已实现结构化检查协议、真实 Python 语法检查示例及日志验证。诊断进入下一次模型输入属于 B20.2，真实修正闭环与组合故障属于 B20.3；目前不能把整章标为完成。

## 两种结果分别记录

编辑得到 applied 表示文件已修改；Hook observed 表示检查器正常交付报告；报告的 failed 表示在指定版本发现问题。三者可以同时成立。检查器进程非零退出、协议损坏或超时仍属于 Hook failed，不能把它解释成文件语法错误或编辑没有发生。原批准、检查点、撤销及存储失败停止策略保持。

## 响应与边界

输入保持 Hook version=1。原前置和后置响应仍可用；新增后置响应 version=2，顶层只接受 version、note、diagnostics。诊断对象字段如下：

| 字段 | 约束 |
| --- | --- |
| checker | 1～64字符检查器名称，字母/数字/下划线/点/横线 |
| path | 本次编辑结果中的原路径，不能替换为其他文件；最多4096字符 |
| expected_version | 已成功编辑的 after_version，64位小写 SHA256 |
| checked_version | 检查器实际读到字节的 SHA256；读取不可靠时为空 |
| status | passed、failed、stale 或 unavailable |
| items | 最多16项；每项 line/column 从1开始，column可为空；severity 为 error/warning/info，code 有界，message最多500字符 |

passed/failed 只能针对 expected_version；failed 至少有一项 error，passed 不得含 error。stale 必须读到不同版本，unavailable 必须没有可靠版本，两者均不提供冒充当前问题的诊断。报告总计最多16KiB UTF-8；仍先受配置中 Hook stdout 上限约束，默认8192字节。任一超限整体拒绝，不悄悄丢诊断；控制字符、非法 Unicode、未知字段和重复键同样拒绝。

宿主 HookRunner 核对报告与 replace_text/apply_proposal 的实际成功结果；SessionLog 写入和重启投影再次核对。日志保留原轮次/请求/工具/Hook关联，不读取文件来改写旧事实。报告是受信本地检查器的观察，格式和来源校验不证明检查器本身正确，也不能授予工具权限。

## 使用示例

[after_python_diagnostics.py](../examples/hooks/after_python_diagnostics.py) 只用标准库，最多读取1MiB并核对版本，用 ast.parse 检查，不执行目标代码。语法错误作为正常协议的 failed 报告返回；文件变化标记 stale，读取失败或过大标记 unavailable。原 [v1示例](../examples/hooks/after_python_syntax.py) 保留，便于比较协议演进。

在已有用户配置中，显式将 hooks.after_tool.argv 指向所用 Python 解释器和新脚本的绝对路径即可；复用原 timeout_seconds 和输入/输出限制，不另加开关。项目不会自动修改真实用户配置。

当前报告可从 SessionStore.load_hooks 或 python -m adam.diagnostics 的既有只读日志入口查看。此小项尚不把报告注入模型，也不声称文件此刻仍是检查时版本。下一小步会处理独立观察、版本新鲜度与上下文预算。

## 验证

[6项业务场景](../tests/features/test_edit_diagnostics.py) 经过模拟模型 HTTP、实际编辑和独立检查进程：正确/错误语法、文件外部变化、读取超限、跨文件伪造及日志来源篡改；目标代码的顶层副作用不会执行，重启不修改已有事件。[22项契约边界](../tests/test_diagnostics.py) 覆盖结构、状态、位置、文本、总字节、来源和旧协议。新增共28项，相关126项通过（45.16秒）；原 Hook 示例与过程测试保持。

初次运行的超大参数自动测试ID导致 Windows 测试环境设置失败，改用短案例名后复验通过；这是测试命名问题。Ruff、557个文件格式、Pyright与注释差异分别核对；主线按 AGENTS.md 三级验证规则执行。没有调用付费 API。
