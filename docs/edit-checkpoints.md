# 修改检查点与撤销

范围：T7.1 的可靠快照与 T7.2 的编辑接入、/undo 已实现；T7.3 仍需完成独立进程中断、重复撤销及编辑→重启→撤销整体验收。需求以 [能力扩展任务](agent-maturity-tasks.md) 为准。

## 使用方式

正常聊天中的 replace_text 获批后，先保存原字节，再记录 edit_started 并写文件。原字节来自同一次规划读取，不从 diff 重建，也不在批准后重新读取一份可能已变化的内容。快照或开始日志失败会停止本轮，不能继续写文件。

输入 /undo 撤销当前会话最近一项尚未撤销的成功单文件编辑。程序不请求模型，显示完整逆向 diff，只有明确批准才执行。plan 模式拒绝撤销；执行期间不能切换模式或重入另一个任务。

当前文件必须仍等于原编辑的修改后版本。批准前和最终替换前分别检查；外部修改、路径权限撤回、凭据或内部目录限制都会阻止覆盖。最新候选的快照缺失或损坏时明确报错，不悄悄选更早的文件。成功撤销后再次请求会选择下一项尚未撤销的编辑；每次独立批准。

撤销只恢复内容，不撤销任意 shell 命令，也不删除或改写原聊天历史、原编辑记录和 token 统计。后续模型修改仍应重新读取文件取得当前版本。用户撤销是本地应用命令，T5 的模型工具前后 Hook 不会为它伪造一组模型请求/工具来源；它仍执行当前模式、写入权限、逐次确认和日志检查。

普通组件直接调用 EditTools 且不提供持久化回调时，不保存检查点；标准聊天工具循环通过 ToolInvocation 提供回调。子 Agent 不能直接编辑或发起 /undo。标准组装将检查点目录加入主/子路径工具、规则/Skill 材料与文档检索的保留目录；已批准通用命令和外部 MCP 服务不因此成为操作系统沙箱。

## 存储契约

EditCheckpoint 固定会话、编辑、轮次、请求、工具调用、目标绝对路径、原始字节、修改前哈希及预期修改后哈希。原字节不经过文本重编码，保留 BOM、换行形式和最后一行。目标路径只是来源数据，读取快照不读取或修改它。

CheckpointStore 使用如下位置；备份会话时需要连同 _checkpoints 一起保留：

~~~text
<会话根目录>/_checkpoints/<session_id>/<edit_id>.json
<会话根目录>/_checkpoints/.locks/<session_id>.lock
~~~

JSON version 为 1，original 使用 Base64，两个版本使用 SHA-256。会话 ID 为 8 位、编辑/轮次/请求 ID 为 32 位小写十六进制；工具 ID 最多 1024 个 UTF-8 字节，目标路径最多 32768 个 UTF-8 字节。格式不接受额外字段、重复字段、不支持的版本、坏编码或不匹配哈希。

调用方注入 max_bytes，沿用编辑文件字节上限；存储层不依赖配置模块。允许范围与现有文件配置一致，为 1～100000000 字节。编码文件读取上限为 Base64 最大长度加 64 KiB 元数据；解码后再次检查原字节大小。下调文件上限后，超过新上限的旧快照也会明确拒绝读取。

## 可靠写入与成功对账

快照读写共用会话级 FileLock。独立临时文件写完后 flush、fsync，再以同目录 os.replace 发布。相同 ID 和相同内容可重复保存，不同内容拒绝覆盖；锁忙或 IO 失败明确报错。只清理本次临时文件，崩溃遗留临时文件不自动加载或发布。

SessionStore.save_edit_checkpoint 从 edit_planned 取得来源及版本，只接受进行中轮次里尚未开始的编辑。load_edit_checkpoint 仅对 status=applied 且来源/路径/版本一致的副本返回内容；undo_checkpoint 还会排除已经撤销的编辑，并阻止未确认的操作。

快照不能证明编辑成功。planned、not_started、unknown、denied、conflict、failed 均不能因快照存在而变成成功；即使文件已变成候选内容，缺少 edit_finished 仍保留 unknown。老会话缺少原始快照时不反向猜测或补造副本。

ID 在拼接路径前校验，存储根不能通过链接重定向，子路径解析后不得越界。路径检查和替换不是跨进程事务；fsync 加原子替换用于进程故障恢复，不承诺所有文件系统掉电时都已持久化目录项。

## 撤销日志与恢复

用户操作独立保存在原会话 JSONL，不创建模型轮次：

| 事件 | 事实 |
| --- | --- |
| undo_requested | 固定 undo_id、原 edit_id、路径、逆向前后版本、source_turn_id/request_id/tool_call_id |
| undo_prepared | 当前版本已校验，保存即将展示的逆向 diff |
| undo_started | 用户已批准，准备执行；缺少结果时为 unknown |
| undo_finished | applied、denied、conflict、failed 或写入前 interrupted；保存耗时和受控错误 |

开始前拒绝、冲突、权限/写入故障分别记录；只有 applied 携带恢复后的版本。开始后不能用 denied/interrupted 覆盖 unknown。文件已恢复但结束日志失败时，保留文件事实并停止，不报告成功、不自动再写一次。

重启取得会话锁后，requested/prepared 通过 session_recovered 的 undo_ids 标记 interrupted；已开始的 unknown 不推断、不自动重跑。尚未完成或结果未知的撤销会阻止新模型请求和手动摘要；尝试发送的输入仍按既有规则保留为失败诊断，不加入有效历史。

原编辑及成功聊天历史保持不变。独立日志诊断在顶层 undos 展示撤销来源及状态；没有新增聊天诊断命令。完整真实进程恢复验收留在 T7.3。

## 验收依据

- [原始快照业务](../tests/features/test_edit_checkpoints.py) 与 [存储边界](../tests/test_checkpoints.py)：T7.1 的字节、版本、锁、故障和重启验证继续保留。
- [编辑与撤销](../tests/features/test_undo.py)：标准组装、模拟 HTTP、真实文件和日志，覆盖逆向确认、最新候选、拒绝、外部冲突、权限/模式、保存失败、取消和 shell 边界。
- [副本访问隔离](../tests/features/test_checkpoint_access.py)：真实主/子工具均拒绝直接读取检查点。
- [撤销状态校验](../tests/test_undo_log.py)：来源、逆向版本、转换顺序、重复终态和未知恢复规则。
- T7.2 新增46项全部通过；原编辑故障、进程强杀和子任务场景另行回归。全量检查随本项提交执行；不调用付费模型。当前模块关系见 [系统架构](architecture.md)。
