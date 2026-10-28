# 通用命令工具

模型提供 argv、cwd 和可选 timeout_seconds；程序负责验证、确认、执行、输出采集与记录。
没有命令种类白名单，也不自动添加测试参数或修复选项。

```json
{"argv": ["git", "status", "--short"], "cwd": ".", "timeout_seconds": 10}
```

```json
{"argv": ["python", "analyze_logs.py", "server.log"], "cwd": "../another-project"}
```

- argv 是非空字符串数组；程序使用绝对路径、相对 cwd 的路径或 PATH 中的名称。
- 不自动切换 Python 虚拟环境，需要时显式提供 `.venv/Scripts/python.exe` 等解释器路径。
- cwd 默认启动工作目录，可以指定其他目录；终端展示绝对目录并逐次确认。
- shell=False，不隐式解释管道和重定向；需要时显式调用 powershell.exe、cmd.exe 等。
- tool_limits.command_timeout_seconds 默认 30 秒，也是单次请求的最大超时。
- 只有输入 y 才执行。状态区分 completed、denied、timed_out、start_failed。
- completed/ok 表示进程正常结束，业务是否成功还须结合 exit_code/stdout/stderr。
- 输出受 max_result_chars 限制，裁剪时保留状态和退出码，持续排空 stdout/stderr。
- 只继承基础环境变量，使用 UTF-8；其他变量可在显式启动的脚本内设置。参数和输出会被记录，勿放入密钥。
- 执行日志独立保存；只有 command_started、缺少 command_finished 的执行恢复为 unknown，阻止自动重跑。
- 当前 v4 保留 v3 引入的全部工具留痕：所有工具都有 tool_started/tool_finished，关联 turn_id、request_id、tool_call_id；命令额外保留 command_* 及 execution_id。tool_started 仅表示进入处理，实际进程边界看 command_*。
- 确认不是系统沙箱，进程拥有当前账户权限；读取工具的路径授权不限制命令内部行为。
- 超时/取消时尽力清理进程树并回收直接子进程，不保证处理主动脱离进程树的进程。

阅读顺序：command_tools.py → registry.py → executor.py → conversation.py → storage.py / session_log.py；终端确认入口在 cli.py。
关联规则和失败示例见 [会话执行轨迹](session-tracing.md)。
测试见 tests/features/test_command_execution.py：不同程序、脚本参数、工作目录、失败、拒绝、超时、输出截断及恢复。

## 阶段归属与验收状态

本工具属于 V4.2 提前完成的基础能力，不表示 V1.3、V2.3 或 V4 整阶段完成。
自动化覆盖本地进程与模拟模型闭环；用户已反馈成功、拒绝、失败三种真实模型回答，
文件编辑与修改后重跑测试闭环尚未实现。V1.3 和 V3 已完成工程验收，V2.3 固定评估仍跳过。
后续按 V4.1a 修改计划、V4.1b 确认写入、V4.1c 模型接入与留痕、V4.2a 有限修正、V4.2b 整体验收推进；通用命令继续复用，完整任务状态见 [练手 TODO](agent-practice-todo.md)。
