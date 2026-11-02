# 新建文件

## B22.1.1 创建计划基础

本节记录最初的规划基础；当前生产create_file已具备规划、批准、发布、日志及撤销。下文按阶段保留当时边界，当前实现和最新进度见B22.3及末尾待办。

| 组件 | 作用与边界 |
| --- | --- |
| [CreatePlan](../adam/contracts/models.py) | 不可变path/candidate/diff；before_version与original固定为None，代表原目标不存在，不能用空字节或空内容摘要代替 |
| [PathPolicy.resolve_create](../adam/tool_calls/policy.py) | 使用原独立写根，拒绝已有目标、凭据、保留目录、.git及链接写入；父目录必须已存在，不自动mkdir |
| [plan_create](../adam/tool_calls/edit_plan.py) | 纯函数生成UTF-8候选、CRLF转LF和完整差异；字节上限沿用max_text_file_bytes，拒绝NUL、孤立CR、非法UTF-8 |
| [EditTools.prepare_create](../adam/tool_calls/edit_tools.py) | 先检查实际路径和当前缺失状态，再生成计划；不调用批准回调、不创建文件 |

允许创建空文件，但计划仍显示`new empty file`及`/dev/null`来源，不能将它当作没有变化。非空diff复用已有差异渲染，缺失末尾换行继续明确展示，写入候选与展示字符串分离。Windows额外拒绝设备名、替代数据流和尾部点/空格别名，避免目标身份含糊。

规划只是观察：用户批准前后仍可能有别的进程创建同名文件。后续实际发布必须重新检查权限并以“不覆盖已有目标”的原子操作完成，不能只靠exists后再普通写入。当前没有把CreatePlan交给原execute_plan，原替换、检查点和撤销接口保持原行为。

## B22.1.2 批准与再次检查

`FileChangePlan`将已有文件修改与新建计划组合为批准回调的输入；两个计划仍分别保留原版本和缺失语义。原bootstrap、工具工厂和CLI共用这一接口，CLI明确显示“创建文件”和完整diff，只接受y/Y；EOF拒绝，Ctrl+C及显示故障继续传播。已有修改入口的行为保持。

[EditTools.confirm_create](../adam/tool_calls/edit_tools.py)在请求批准前检查当前路径，批准后再检查一次：目标必须仍缺失、父目录仍存在、当前策略仍允许写入。没有回调默认拒绝，拒绝不会创建占位或临时文件。返回True只是批准固定候选，不代表磁盘成功，也不阻止其他进程随后抢先创建。后续发布仍必须原子拒绝覆盖；本阶段没有将新建入口提供给模型，也没有写入编辑日志。

[12项批准场景](../tests/features/test_file_creation_approval.py)验证原文展示、默认/显式拒绝、EOF、取消、回调故障，以及批准前后同名出现、父目录删除和宿主撤销写权限。连同创建规划、原编辑闭环、撤销与架构共105项通过（27.76秒）；Ruff、580文件格式和Pyright通过。五处已有源码的注释/类型差异和新增测试全文已审查，原批准范围、非y拒绝和取消说明仍保留。主线按 AGENTS.md 三级验证规则执行。

## B22.2.1 原目标缺失的检查点

[EditCheckpoint](../adam/contracts/checkpoints.py)增加operation：原replace必须保留真实字节与对应哈希，create则必须同时使用`original=None`、`before_version=None`。目标版本仍是预期候选的SHA-256，即使候选为空也不能把原状态写成空文件摘要。

[CheckpointStore](../adam/session/checkpoints.py)将create保存为明确带operation和null的v2记录；原replace继续写入和读取原v1，不改既有快照格式。相同ID内容不同仍拒绝覆盖，读取仍核对身份/字段/来源，临时文件和保存失败不能变成可用检查点；存储不会读取或创建目标文件。

[EditLog](../adam/session/edit_log.py)支持纯创建事件投影，开始但未完成仍unknown，不能靠快照猜成功；操作类型不允许中途切换。SessionLog额外核对操作与父工具，原replace_text/apply_proposal不能伪造create。当前创建工具尚未注册，也尚未接入SessionStore保存链。原plan_restore明确拒绝缺失快照，防止把创建撤销错做成写入空文件；实际安全删除将在B22.2.3实现。

[17项检查点场景](../tests/features/test_creation_checkpoints.py)覆盖真实保存/重启/幂等、旧v1与空字节区别、损坏/歧义拒绝、发布故障、纯创建/逆向日志及真实会话父来源拒绝。相关106项通过（28.68秒），Ruff、581文件格式和Pyright通过。新增测试全文、五处源码差异和注释已审查；字节保真、来源、unknown以及保存不代表完成的说明保留。主线按 AGENTS.md 三级验证规则执行。

## B22.2.2 完整候选发布

[EditTools.apply_create](../adam/tool_calls/edit_tools.py)是接受已批准固定计划的宿主API：复核权限/缺失→同目录临时文件写完整→flush/fsync→再次复核→`os.link`发布新名字→清理临时名字。实际同名文件或目录已经出现时，内核操作失败，不覆盖；不支持硬链接或发生IO错误时明确失败，不退回普通写入、rename或replace。

这个选择要求文件系统支持硬链接。[Python os.link文档](https://docs.python.org/3.11/library/os.html#os.link)提供跨平台接口；[Windows CreateHardLinkW文档](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw)说明同卷限制和共享文件属性，因此临时文件放在同目录，发布后清理也不chmod，否则会改变目标属性。本轮在Windows实际文件系统验证了发布及已存在目标的失败，不宣称已覆盖所有网络盘和平台。

临时名清理失败不掩盖原结果，可能遗留指向同一文件的名字，程序不自动当成第二次创建或恢复。flush/fsync保证先完成候选同步，这里没有目录fsync或断电持久性验收。路径前后校验也不是抵御恶意目录替换的系统沙箱；本步保证普通同名创建竞争不覆盖，沿用原权限边界。

[12项原子创建场景](../tests/features/test_atomic_file_creation.py)包含空文件、UTF-8/LF、64KiB完整候选，第二次检查后真实创建同名文件/目录再调用os.link，以及写入/flush/fsync/link失败、取消、清理失败和禁止覆盖回退。相关108项通过（23.08秒）；Ruff、582文件格式和Pyright通过。新增方法契约、原有编辑差异及测试全文已审查，原注释未删除。初次大参数自动用例名导致初始化报错，改为显式短ID后通过，生产边界未放宽。

目前尚未将apply_create接入SessionStore编辑链或模型工具，创建日志、检查点关联和安全撤销在下一步完成；独立进程同名竞争及真实强杀验收留在B22.3。

## B22.2.3.1 创建进入原编辑记录

`EditTools.execute_plan`现在接收CreatePlan，记录明确operation=create，复用原批准→检查点→开始→发布→结果顺序。`ToolInvocation`仍用原动作回调，原字节或None只传给检查点保存，不把文件副本塞进JSONL；`SessionStore`从已校验计划取得操作、来源与版本，读取时再核对快照与成功事实。

create_file加入可信编辑名称集合，使用原编辑尝试额度与执行事实。生产工具工厂尚未注册创建；本步测试宿主显式注册处理函数，再穿过真实Conversation、ToolExecutor、原批准、文件和会话存储，远端HTTP使用替身。创建撤销和正式模型入口留在后续。

[9项创建记录测试](../tests/features/test_creation_records.py)验证空文件/UTF-8、真实创建与缺失检查点重启、明确拒绝、计划/快照/开始/结束保存故障、最后一刻真实同名冲突，以及编辑额度。结果保存失败时文件可能已经存在，日志保持unknown；重启不重建、不拿磁盘内容猜成功，阻止继续副作用。批准前后的预检失败尚未进入edit_started，工具错误保留原因，重启将该计划记为not_started。

相关79项通过（24.62秒），Ruff、583文件格式、Pyright通过。三处源码差异与新增测试全文已审查，原批准、原字节保真、检查点先于写入、日志失败停止及unknown说明保留；存储说明同步扩展为字节或明确缺失。主线按 AGENTS.md 三级验证规则执行。

## B22.2.3.2 撤销至缺失状态

原`UndoService`仍从最近成功且未撤销的编辑读取可信检查点；创建的original=None时，`plan_restore`先核对当前路径和创建后内容SHA-256，再生成不可变DeletePlan。差异以`+++ /dev/null`表示目标消失，空文件也显示delete empty file；不会伪造写入空字节的恢复计划。

终端明确显示“删除文件”和完整差异，仍只接受y/Y。批准后`EditTools.apply_restore`重新核对独立写权限、普通文件和内容版本，再unlink；原替换撤销继续调用原apply。没有增加模型通用删除入口，DeletePlan不进入创建/替换的execute_plan。检查点不授予当前写权限，也不允许跳过已观察到的外部修改。

原undo_*独立日志保留创建来源、逆向版本和明确None缺失目标。拒绝后可显式重试，取消在开始前记中断，删除错误记失败。删除已经发生而结束记录保存失败时保留unknown，重启不猜结果或重删。内容复核和unlink不是跨进程比较删除事务，无法保证另一进程恰在复核后改变目标时不发生竞态；沿用原编辑对协作目录的权限边界，不声称提供文件系统沙箱或全局写锁。

[10项创建撤销测试](../tests/features/test_creation_undo.py)使用实际创建/批准/存储，验证空与非空文件删除、拒绝后显式重试、重复撤销拒绝、批准前后外部内容、撤权/取消、unlink失败，以及prepared/started/finished保存故障后恢复。相关114项通过（31.54秒）；批准展示分支改为清晰if/elif后22项通过（6.34秒）。Ruff、584文件格式与Pyright通过。原“缺失不能当空文件恢复”场景保留名称，更新为DeletePlan无副作用断言，未减少或跳过原场景。

六处源码/已有测试差异和新增测试全文已审查。原批准、版本复核和末尾换行说明已扩展；曾经“缺失恢复尚未接入”的拒绝分支被已验证的删除计划替换，其防止空字节误恢复的目的仍保留。UndoService模块说明调整为权限与版本复核，避免将删除误称为原子比较删除。架构图同步，主线按 AGENTS.md 三级验证规则执行。

## B22.3.1 正式工具与组合流程

生产工具工厂注册`create_file(path, content)`，归入editing并声明EDIT_FILE。它复用原按需发现和每次请求的固定工具名单：同批先发现再调用仍被拒绝；plan模式、参数和路径规则在批准前检查。完整候选与diff逐次批准，独立读权限不隐含创建权，子Agent只能返回approval_required，不弹出批准或写文件。

新入口只规划缺失目标后调用原execute_plan，原日志、检查点、编辑次数、失败状态和撤销链继续使用。创建完成后read_file取得版本，再用replace_text修改；连续两次用户undo分别恢复创建内容和删除创建文件。没有增加模型通用删除工具。

诊断来源匹配改用已有FILE_EDIT_TOOLS，仍要求实际applied结果、同路径及同版本。三个仓库Hook示例显式纳入create_file，前置目录拒绝不调用批准；后置AST不会执行目标代码。语法失败与创建成功分别记录，结构化问题进入下一次请求；用户个人脚本和配置不自动改写。

[10项生产流程测试](../tests/features/test_file_creation_loop.py)验证上述闭环、模式/只读授权/非法参数/拒绝、真实目录和两种语法Hook、子Agent限制，以及删除源目标后离线重放仍不重建。相关112项通过（72.94秒），Ruff、585文件格式与Pyright通过。初次断言误以为原替换日志也有operation字段，修正为保留原缺省replace语义后通过，没有改旧记录格式。

新增测试全文和源码/示例/旧测试差异已审查；原批准、版本、Hook不替代权限及不执行目标代码的说明保留。旧生产目录“不开放”断言改为“已注册且默认拒绝”，对应业务变化改名，9项原场景仍保留。架构同步当前注册与诊断关系；主线按 AGENTS.md 三级验证规则执行。真实独立进程竞争与强杀留在B22.3.2。

## 验证与下一步

[15项业务场景](../tests/features/test_file_creation_plan.py) 使用真实临时目录验证UTF-8/LF和完整diff、候选固定、缺失/空文件区别、内容和字节上限、已存在目标/目录、父目录不自动生成、读写权限独立、凭据/保留路径和Windows特殊名称。规划不会调用确认回调，也不生成占位或临时文件。

连同原编辑、撤销与架构共84项通过（23.16秒）；Ruff、578个文件格式与Pyright通过。新增测试全文和四处源码差异已审查；原末尾换行、展示与字节分离、版本和权限说明保留，只扩展创建含义。主线按 AGENTS.md 三级验证规则执行。

- [x] B22.1.1：缺失状态、纯候选/diff与权限规划。
- [x] B22.1.2：统一批准对象和创建前状态复核。
- [x] B22.2：原子创建、日志、检查点及撤销；批准后竞态出现文件不得覆盖。
  - [x] B22.2.1：缺失状态快照与纯创建日志契约。
  - [x] B22.2.2：原子发布，不覆盖竞态目标。
  - [x] B22.2.3：真实创建留痕和版本匹配的批准撤销。
    - [x] B22.2.3.1：创建接入原执行器与保存/恢复链。
    - [x] B22.2.3.2：版本匹配并批准后撤销至缺失。
- [ ] B22.3：真实工具闭环、拒绝、权限、同名竞争、写失败和撤销验收。
  - [x] B22.3.1：生产工具、模式/权限/Hook与真实创建/修改/撤销/重放。
  - [ ] B22.3.2：独立进程同名竞争及实际强杀后的创建/撤销恢复。

进度见 [完整路线](agent-book-practice-plan.md)，调用关系统一见 [当前架构](architecture.md)。
