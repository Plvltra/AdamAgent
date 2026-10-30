# 大工具结果附件与分段读取

状态：T8.1～T8.3 存储、主/子工具循环接入与整体验收已完成。主需求见 [能力扩展任务](agent-maturity-tasks.md)。

## 目标与阶段边界

长输出先可靠保存，模型只接收简短预览与固定引用，需要时再读指定范围。附件不能绕过会话隔离、原有权限和上下文预算；保存不完整或失败时必须如实报告。

T8.1 提供 ArtifactSource、ArtifactRef、ArtifactPage 和 ArtifactStore。T8.2 通过 ToolArtifacts 在工具动作结束、普通结果截断之前保存长结果；ToolInvocation 负责调用，Conversation 不按工具名称增加分支。短结果沿用原编码器，read_artifact 结果已经分页，不递归产生附件。

附件正文是原工具结果的 JSON 文本，包含命令 stdout/stderr 和实际状态，不是任意路径下载接口。保存的是本次取得的结果；原生 read_file、Git 等工具已有的分页和上游截断保持，不能据附件完整标志声称读取了整个文件或仓库。

## 模型交互与配置

[artifacts] 默认开启，参数见下表及 config.example.toml；enabled=false 时不注册 read_artifact，也不扩大命令捕获范围。正文目录只在实际产生附件时创建。

1. 工具结果 JSON 超过 tool_limits.max_result_chars 时尝试保存正文。
2. 模型收到有界预览与 artifact：artifact_id、version、saved_chars、complete、format=json。命令仍保留退出码与状态；非零退出不会被描述为检查通过。
3. read_artifact 接受 artifact_id、version，以及可选 offset/count；当前会话由宿主绑定，不接受 session_id 或文件路径。返回 text、offset、next_offset 和完整性。
4. 分页同时服从 max_read_chars 与实际 JSON 编码后的 max_result_chars。转义导致进一步缩短时同步 next_offset，不跳过字符；读取内容进入原工具消息，因此继续受工具次数与上下文预算约束。
5. 正文先发布，再追加 artifact_saved 来源事件，最后保存 tool_finished 和执行后置 Hook。事件核对正在进行的轮次、成功请求、实际工具名称与调用 ID；同一调用只保存一份引用。

命令每条流最多捕获 artifacts.max_bytes 个字节，再持续排空其余管道；单份与会话保存配额仍独立检查。UTF-8 解码替换、管道未完整读取或捕获截断均标为不完整。command_finished 只保存有界事实预览；扩大捕获不会把长输出再次写入 JSONL，也不修改供附件保存的原结果。

正文存储失败返回 artifact_unavailable，不提供虚假引用，保留动作实际状态且不重跑。正文已发布而来源日志失败时停止本轮；孤立文件不能凭文件存在直接读取。读取先查当前日志，再比较正文与记录中的完整引用，包括来源、版本、大小和完整性；丢失、篡改或旧版本只返回工具错误，不重做原命令。日志本身损坏或读取故障仍停止。

## 数据契约

| 数据 | 含义 |
| --- | --- |
| ArtifactSource | session_id、turn_id、request_id、tool_call_id、tool_name；只有来源数据，不创建轮次 |
| artifact_id | 32 位小写十六进制 ID，缺省由程序生成；已有 ID 不再写入，即使内容相同也拒绝 |
| version | 已保存 UTF-8 正文的 SHA-256；不把它当原始完整输出的版本 |
| saved_bytes / saved_chars | 实际保存的 UTF-8 字节数和 Unicode 字符数 |
| total_bytes | 取得完整原文本时记录其字节数；上游已经截断时为 null，不猜总量 |
| complete | 仅在上游完整且全部文本已保存时为 true |
| ArtifactPage | 固定引用、本页起点、文本和 next_offset；null 表示已保存部分读完 |

会话 ID 为 8 位，轮次和请求 ID 为 32 位小写十六进制。工具调用 ID 最多 1024 个 UTF-8 字节、名称最多 256 字节，不接受空白值或 NUL。契约不依赖文件、模型或配置模块；调用方须另行核实来源对应真实执行记录。

## 存储接口

- save(source, text, artifact_id=None, source_complete=True)：原子保存并返回引用；失败抛出 StorageError，不返回未落盘的引用。
- describe(session_id, artifact_id)：验证文件身份、格式、正文大小及哈希后返回引用。
- read(session_id, artifact_id, expected_version, offset=0, count=None)：按字符读取固定版本，返回 ArtifactPage。count 缺省采用单页上限。

offset 从 0 开始，单位为 Python Unicode 字符，不是 UTF-8 字节或终端显示宽度。offset 等于已保存字符数时返回空页；负值、越界、布尔值或超过单页上限的 count 拒绝。每页都验证有界的完整附件，再取所需文本；本阶段不做索引或流式随机 IO。

读完不完整附件仍保持 complete=false。版本不匹配、来源与目录身份不符、内容损坏或缺失时拒绝读取，不跳过错误、不自动修复、不切换到另一个会话寻找同名 ID。

## 默认限制与配额

这些参数同时用于 [artifacts] 配置与存储构造；配置只在组装层拆分，存储不依赖配置模块：

| 参数 | 缺省值 | 约束 |
| --- | --- | --- |
| max_bytes | 1 MiB | 单份实际保存的 UTF-8 正文上限 |
| max_session_bytes | 16 MiB | 同一会话全部已发布正文的总字节上限 |
| max_items | 128 | 同一会话已发布附件数量上限 |
| max_read_chars | 4000 | 单页返回的字符数上限 |

上限均须为正整数，拒绝 bool；配置允许的硬上界依次为 100000000 字节、1 GiB、4096 份和 1000000 字符。JSON 转义和元数据另受 max_bytes × 6 + 64 KiB 的编码读取上限限制；配额扫描最多访问 max_items × 2 + 32 个目录项。

只剩部分容量时保存有效 UTF-8 前缀，字符末尾不会被拆断，并返回 complete=false。容量耗尽、数量已满，或连第一个完整字符都放不下时明确失败。上游不完整的文本即使全部存下，也仍然不完整。

会话配额在锁内核算；坏附件不能被忽略以腾出额度。不同会话各自计数。附件内容不自动删除或覆盖，本阶段不增加清理服务。

## 文件格式与可靠性

主应用使用 <sessions_dir>/_artifacts；子任务使用该目录的 children/<主会话ID> 子目录。子任务之间仍按各自会话 ID 隔离，主/子短 ID 即使相同也不共用目录。所有主/子普通文件、Git、规则、Skill 和文档检索都保护整个附件根；已批准的任意命令仍不是操作系统沙箱。

调用方传入对应附件根目录，结构为：

~~~text
<附件根>/<session_id>/<artifact_id>.json
<附件根>/.locks/<session_id>.lock
~~~

单份 JSON 同时保存 format_version=1、reference 和 text。拒绝重复字段、未知字段、坏编码、不支持版本、身份/大小/哈希不一致及非法完整性状态。完整文本保留 BOM、CRLF、控制字符和末行内容。

同一会话的保存和读取共用 FileLock。保存先写同目录临时文件，flush/fsync 后原子发布；故障只清理本次临时文件，已有附件保持。孤立临时文件不作为附件，不因重启而自动发布。仅在当前受控写入流程内保证不覆盖已有 ID；外部替换内容后，持旧版本的读取仍会失败。

目录和文件链接重定向均拒绝，包括指向另一会话的链接。这里的隔离是存储身份、宿主来源与路径检查，不是操作系统沙箱。

备份必须保留整个 sessions_dir，包含 _artifacts 及其 children 子树；只有 JSONL 而没有正文不能恢复附件。停止所有写入后再复制，见 [备份指南](install-and-recovery.md)。

## 验收与后续接入责任

- [附件存储业务](../tests/features/test_artifact_storage.py)：长 Unicode 文本重启后精确拼回、单份/会话/数量限制、上游不完整、旧引用与外部替换、跨会话拒绝、真实 IO 故障、锁与并发配额、孤立临时文件。
- [格式边界](../tests/test_artifacts.py)：来源、非法范围、14 种损坏输入、无效文本/ID 与不可变引用。
- T8.1 新增45项与19项架构约束共64项定向检查通过；Ruff、Pyright 与完整回归已通过，提交 9edbd4e。链接重定向测试模拟真实路径解析结果，不声称测试了 Windows 创建链接的权限。

T8.2 新增34项验收：实际命令长日志及尾部读取、通用目录结果、计划模式和重启读取、正文/来源保存失败不重跑、篡改/丢失/旧版本拒绝、转义分页、捕获与配额不完整、开关及非法参数、真实主/子隔离。入口见 [工具附件](../tests/features/test_tool_artifacts.py)、[主子边界](../tests/features/test_artifact_subagents.py)、[参数及关闭](../tests/features/test_artifact_arguments.py)、[配置与来源](../tests/test_artifact_integration.py)。

首轮业务测试的预期忽略 Windows 文本 stdout 的换行转换，长重复字符串差异比较耗时；命令改用显式 UTF-8 字节输出，先核对长度再比较内容，未放宽产品行为或断言。已有存储、架构、命令、子任务91项回归通过；完整 Ruff 格式/lint、Pyright 和 pytest 已通过，T8.2 提交为 5ff9049。

## T8.3 整体验收记录

2026-10-30，新增8项定向验收通过（25.12秒），没有调用付费模型。测试经过标准应用组装、真实文件/命令与实际 JSONL；只有模型响应和明确故障边界受测试控制。产品代码无需改动。

| 相同任务的输入方式 | 模型请求数 | 累计输入字节 | 累计本地输入 token 估算 | 命令次数 | 答案 |
| --- | ---: | ---: | ---: | ---: | --- |
| 全量工具输出 | 2 | 67058 | 22160 | 1 | 42 |
| 附件预览与尾部分页 | 3 | 26973 | 8680 | 1 | 42 |

任务读取同一份47509字节日志中的 ANSWER 值。输入字节是各次实际请求 messages 与 tools 的 JSON UTF-8 字节之和，不含认证头；token 为现有 ContextBuilder 的估算。该环境下字节减少约59.8%，估算减少约60.8%，同时多了一次模型请求。假模型根据已收到的结果选择已知尾部并提取答案，因此只证明流程与输入开销，不证明真实模型能自行定位或同样节省计费。

[任务对照与故障测试](../tests/features/test_artifact_acceptance.py) 覆盖：

- read_artifact 返回的4000字符进入当前轮受保护输入，后续请求因预算不足被拦截；此前请求的已知消费、原成功历史和附件保留。
- 在实际 os.replace 发布调用注入 PermissionError：新结果退化为预览，既有附件字节不变，不留下临时文件，不重跑命令。
- 停止写入后复制完整 sessions 目录，在新目录恢复并按原版本读取；原目录不变，新目录不产生旧命令副作用。

[真实进程恢复测试](../tests/features/test_artifact_recovery.py) 与 [子进程入口](../tests/features/support/artifact_worker.py) 使用本地 TCP 模型服务；独立进程实际执行命令，再由父进程强杀。四个边界分别为正文发布后、artifact_saved 半行、来源完整保存后、tool_finished 后。半行确实写入并 fsync，强杀不走异常回滚；父进程只读取已退出进程的日志，避免 Windows 句柄影响原子替换。

重新启动全新进程后验证：成功历史与12个已知消费 token 保持；请求已成功、命令退出0，整轮为中断；恢复前日志备份逐字节保留，正文不改变，不请求模型或重跑命令。只有完整来源已保存的两种情况可在随后显式新轮读取，孤立正文仍被拒绝。工具结束记录是否存在单独判断，不能据此把整轮升级为成功。

这些测试不代替真实模型效果评估；完整 Ruff 格式/lint、Pyright 与 pytest 随本项独立提交执行。后续进入 T9 调用录制与离线重放。
