# 业务功能测试任务

更新日期：2026-10-28

这里按用户可见的能力组织自动化验收。功能模块改名后，仍能按“会话恢复”“命令执行”等业务名称找到对应测试。每个文件包含可独立运行的 pytest 场景；参数化场景展开为多个用例。

## 已实现功能与测试入口

| 功能点 | 要验收的业务结果 | 测试文件 |
| --- | --- | --- |
| 多轮聊天与模型切换 | 历史正确传递，新会话隔离；切换模型保留历史；终端指令不作为用户问题发给模型 | [test_conversation.py](test_conversation.py) |
| 配置启动 | 两份 TOML 与命令行覆盖可用；主配置或模型配置错误时在创建 HTTP 客户端前停止 | [test_startup_configuration.py](test_startup_configuration.py)、[test_session_lifecycle.py](test_session_lifecycle.py) |
| 会话保存与恢复 | 跨启动恢复、不串话、失败切换不丢当前会话；回答已生成但保存失败时仍可见且不重试 | [test_session_lifecycle.py](test_session_lifecycle.py) |
| 工具调用与预算 | 模型请求工具、执行后回传、再回答；工具错误可修正；单批额度不足不执行；同轮 ID 唯一、跨轮可复用 | [test_tool_loop.py](test_tool_loop.py) |
| 通用命令 | 确认后才产生副作用，拒绝不执行；退出码、超时、输出限制、环境变量隔离与执行日志符合契约 | [test_command_execution.py](test_command_execution.py) |
| 终端角色与按需用量 | 默认不刷 token，/usage 不请求模型且跟随当前会话；颜色只影响前缀，重定向退回纯文本 | [test_terminal_interaction.py](test_terminal_interaction.py) |
| V1.3.1 执行轨迹 | 失败也保留输入/请求/全部工具结果；关联正确，日志失败停止后续动作，诊断不污染成功历史 | [test_execution_trace.py](test_execution_trace.py) |

这里也保留直接验证业务边界的场景，例如命令超时、请求额度耗尽；不强求每个场景都从 CLI 启动。
基础校验仍在上层：`tests/test_config.py`、`tests/test_llm_config.py`、`tests/test_llm.py`、`tests/test_storage.py`、`tests/test_tools.py`。细粒度协议、路径和数据校验不重复复制到业务测试中。

## 本次补充的任务

- [x] 配置错误时，真实 main 入口在 HTTP 客户端创建前失败。
- [x] /usage 在会话切换和重开后显示所属会话的用量。
- [x] 命令批准/拒绝通过临时文件是否真实写入验证，而不只检查返回文字。
- [x] 目录/文件工具报错后，模型收到错误并改用正确参数完成任务。
- [x] 最终回答的用量日志保存失败时，显示未保存回答，重开后保留未知用量。
- [x] 命令已执行但结束日志写入失败时，不再次执行、不请求模型；重开后结果未知。
- [x] 一次请求多个工具而剩余额度不足时，整批工具均不执行。
- [x] 不同成功轮次复用相同工具调用 ID，记录和恢复仍分别归属正确轮次。
- [x] 子进程不能读取父进程设置的测试用 API 密钥环境变量。

验收结果（2026-10-28）：原有 125 个用例全部保留，新增 11 个用例；全量 **136 passed in 22.72s**。

## 怎样运行

在项目根目录使用项目虚拟环境：

```powershell
# 查看业务测试任务，不执行
.\.venv\Scripts\python.exe -m pytest tests/features --collect-only -q

# 执行全部业务功能验收
.\.venv\Scripts\python.exe -m pytest tests/features -v

# 只验收失败轨迹
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_trace.py -v

# 全量回归，包含基础校验
.\.venv\Scripts\python.exe -m pytest -q
```

pytest 已配置扫描整个 tests；现有 pre-commit 的 pytest 钩子会自动收集这个目录，无需额外配置或手工注册。

## 模拟范围与完成含义

模型 HTTP 响应是预设的，测试不会消耗 API 额度；文件、JSONL、锁和部分子进程会在临时目录实际运行。故障测试在指定边界注入错误，验证程序停止或保留记录的行为。

这些场景不能证明真实模型能正确决定是否使用工具，也不能代替真实进程强制终止验收或用户对配色的辨识体验。V1.3.2/V1.3.3、V3、文件编辑与流式取消等未实现功能的验收仍在 [练手 TODO](../../docs/agent-practice-todo.md)；后续补测规则见 [AGENTS.md](../../AGENTS.md)。
