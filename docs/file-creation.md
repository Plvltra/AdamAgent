# 新建文件

## B22.1.1 创建计划基础

当前仅提供宿主规划接口，尚未增加模型可调用的创建工具或实际写入。目标是先得到可审核的完整候选与diff，之后再接入原批准、编辑记录和安全撤销。

| 组件 | 作用与边界 |
| --- | --- |
| [CreatePlan](../adam/contracts/models.py) | 不可变path/candidate/diff；before_version与original固定为None，代表原目标不存在，不能用空字节或空内容摘要代替 |
| [PathPolicy.resolve_create](../adam/tool_calls/policy.py) | 使用原独立写根，拒绝已有目标、凭据、保留目录、.git及链接写入；父目录必须已存在，不自动mkdir |
| [plan_create](../adam/tool_calls/edit_plan.py) | 纯函数生成UTF-8候选、CRLF转LF和完整差异；字节上限沿用max_text_file_bytes，拒绝NUL、孤立CR、非法UTF-8 |
| [EditTools.prepare_create](../adam/tool_calls/edit_tools.py) | 先检查实际路径和当前缺失状态，再生成计划；不调用批准回调、不创建文件 |

允许创建空文件，但计划仍显示`new empty file`及`/dev/null`来源，不能将它当作没有变化。非空diff复用已有差异渲染，缺失末尾换行继续明确展示，写入候选与展示字符串分离。Windows额外拒绝设备名、替代数据流和尾部点/空格别名，避免目标身份含糊。

规划只是观察：用户批准前后仍可能有别的进程创建同名文件。后续实际发布必须重新检查权限并以“不覆盖已有目标”的原子操作完成，不能只靠exists后再普通写入。当前没有把CreatePlan交给原execute_plan，原替换、检查点和撤销接口保持原行为。

## 验证与下一步

[15项业务场景](../tests/features/test_file_creation_plan.py) 使用真实临时目录验证UTF-8/LF和完整diff、候选固定、缺失/空文件区别、内容和字节上限、已存在目标/目录、父目录不自动生成、读写权限独立、凭据/保留路径和Windows特殊名称。规划不会调用确认回调，也不生成占位或临时文件。

连同原编辑、撤销与架构共84项通过（23.16秒）；Ruff、578个文件格式与Pyright通过。新增测试全文和四处源码差异已审查；原末尾换行、展示与字节分离、版本和权限说明保留，只扩展创建含义。主线按 AGENTS.md 三级验证规则执行。

- [x] B22.1.1：缺失状态、纯候选/diff与权限规划。
- [ ] B22.1.2：统一批准对象和创建前状态复核。
- [ ] B22.2：原子创建、日志、检查点及撤销；批准后竞态出现文件不得覆盖。
- [ ] B22.3：真实工具闭环、拒绝、权限、同名竞争、写失败和撤销验收。

进度见 [完整路线](agent-book-practice-plan.md)，调用关系统一见 [当前架构](architecture.md)。
