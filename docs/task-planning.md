# E3 显式任务计划

更新：2026-10-29。主任务以 [练手 TODO](agent-practice-todo.md) 的 E3 为准。
当前 E3.1 已实现记录与存储；模型工具、终端和整体验收尚未接入。

## 已确认范围

- 多步骤任务由模型按需调用计划工具；简单问答跳过，用户可以要求先列计划。
- 每个用户轮次一份计划；历史留在会话 JSONL，下一轮可参考但不继承待办状态。重启只读恢复，不自动重跑。
- 完成步骤必须关联本轮真实工具结果。程序验证来源及执行状态，不声称能判断语义证据充分性。
- 建立或更新时显示简短清单，/plan 查询当前会话最近计划；第一版仅主 Agent 维护计划。
- 有未闭合步骤却准备最终回答时最多提醒一次，仍使用现有请求额度；再次遗漏则明确显示未完成，保留回答。
- 默认最多 8 步、同时 1 步进行中、每轮重规划 3 次，数量可配置。增删、改写、重排或重开已结束步骤计重规划；普通进度更新不计，旧版本和原因保留。
- E3.1～E3.4 每步验证并单独提交，作者 Plvltra。仅修改 Adam；Adam2 的 E2 保持独立 worktree，集成时审查共享接点，不整文件覆盖。

## 分阶段交付

| 子任务 | 内容 | 状态 |
| --- | --- | --- |
| E3.1 | 数据契约、计划版本、轮次归属、存储失败保护和重启读取 | 已实现 |
| E3.2 | update_plan 工具、配置及真实执行证据校验 | 待实现 |
| E3.3 | 展示、/plan、有限重规划与收尾提醒 | 待实现 |
| E3.4 | 两文件业务验收及普通循环/显式计划对照 | 待实现 |

## E3.1 记录与恢复

contracts/planning.py 定义不可变步骤、完整快照及查询报告；session/plan_log.py 校验版本和重规划次数。SessionLog 校验更新所属轮次、请求和正在执行的 update_plan；SessionStore 复用原追加、文件锁与 fsync 回滚。

plan_updated 包含 turn_id、request_id、tool_call_id、version、steps、reason、replan_count、max_steps、max_replans。步骤包含稳定 step_id、content、status、evidence、reason。状态为 pending、in_progress、completed、failed、cancelled；失败和取消必须有原因，完成必须带证据引用。E3.2 将补引用的真实结果校验。

旧 v4 会话没有 plan_updated 仍可读；含新事件的日志不保证被旧程序读取。计划记录不自动加入成功消息历史。PlanReport 单独携带轮次终态；轮次被中断时保留原步骤，不伪造完成。新会话不会带入旧计划。

验收：[计划存储](../tests/features/test_task_plan_storage.py) 覆盖重启、版本、隔离、非法状态/关联、重规划上限和实际 fsync 失败。
