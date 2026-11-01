# 二进制媒体证据（C00.2）

最近核对：2026-11-01。已实现独立存储和可信宿主 API，不把它等同于图片问答、浏览器下载或多模态模型接入。C02.3 需要可校验下载引用，因此在 C04 前先补本项；C02.3 现已通过 [显式浏览器下载](browser-observation.md) 消费该宿主边界；C00.3 已接 [模型输入与媒体冻结回放](multimodal-input.md)，C04 的图片加载/解码与真实质量仍单独验收。

## 已实现入口

- `MediaSource` / `MediaRef` / `MediaContent` 位于 [contracts.media](../adam/contracts/media.py)，不访问文件或 SDK。
- [MediaStore](../adam/session/media.py) 提供 `save(source, data, mime_type)`、`describe(session_id, media_id)`、`read(session_id, media_id, expected_version=...)`；调用者注入根目录与配额，不接受模型提供的文件路径。
- [SessionMedia](../adam/session/media_evidence.py) 是真实来源与当前会话边界：`publish` 先核对来源，完整发布字节，再提交引用；`read` 先查宿主当前会话日志，再核对整个文件引用和字节。能力工具的薄绑定应借用此宿主边界，不能只因 MediaStore 中有文件就向模型交付。
- `SessionStore.validate_media_source` / `append_media` / `load_media` 和 [MediaLog](../adam/session/media_log.py) 负责严格关联；普通启动不创建媒体正文，只有实际发布才创建存储目录。本项不新增模型工具、媒体输入 CLI 或远端模型请求。

宿主必须已选中对应会话。`publish` 和 `read` 在文件 IO 返回后再次核对当前所有者，期间发生切换则停止，不隐式切回旧会话、提交到新会话或向新所有者交付字节。宿主来源不是模型可自行声明的授权，接入调用方仍负责原文件选择/读取权限、工具批准及取消。

## 引用与来源契约

| 字段 | 语义 |
| --- | --- |
| media_id | 程序生成的 32 位小写十六进制身份；不可覆盖，不是路径或下载 URL |
| source.session_id / turn_id | 所有者和真实输入轮次；长度分别为 8 / 32 位小写十六进制 |
| source.kind=input | 用户在本轮模型请求前提供的媒体；request_id / tool_call_id / tool_name 必须为 null |
| source.kind=tool | 当前进行中工具的确切 request_id、tool_call_id、tool_name；请求须成功，工具尚未结束 |
| mime_type | 无参数、无通配符的小写具体 MIME 子集，例如 image/png、audio/wav、application/octet-stream |
| size_bytes | 实际完整保存的正文长度；二进制不截断、不以残片冒充有效图片或声音 |
| version | 完整原字节的 SHA-256；相同字节可有不同来源/ID，但每份引用均固定 |
| created_at | 本次保存时的 UTC ISO 时间，不冒充文件原创建时间或设备采集时刻 |
| schema_version | 引用版本，当前为 1；未知版本明确拒绝 |

MIME 只是声明。存储层不执行脚本、不解码图片/音频、不验证图片尺寸或音频时长，也不保证任意声明的格式能被解码。后续视觉、音频和视频适配器须独立限制格式、解码资源及模型能力。`MediaContent` 含真实 bytes，只在可信宿主内传递；不直接序列化到 JSONL 或模型文本。

`media_saved` 只保存完整 MediaRef；严格拒绝额外字段、内联 data/base64、任意路径、坏版本和错误来源。来源阶段在追加前再次校验；同一工具可产生多份媒体，重复 ID 一律拒绝。已有 JSONL v4 文本会话继续读取，新增媒体事件由当前版本显式处理，旧程序遇到未知事件不能假装兼容。

## 目录、原子性和限制

默认由实际消费者使用 `<sessions_dir>/_media`，结构为：

~~~text
_media/<session_id>/<media_id>.media
_media/.locks/<session_id>.lock
~~~

每个 `.media` 文件包含固定 magic/文件版本、4 字节大端 JSON 头长度、有界 UTF-8 引用头和**原二进制正文**，不把正文编码为 base64。头上限 32 KiB；未知 magic、重复 JSON 字段、超大长度、坏来源、正文长度/摘要不符均拒绝。读取分配上限来自可信构造参数，而非磁盘自报长度。

默认配额：单份 10 MiB、同会话正文总量 64 MiB、128 份；构造硬上界分别为 100000000 字节、1 GiB、4096 份，均拒绝 bool/非正整数。正文和数量在跨实例会话锁内核算；达到限制拒绝整个新媒体，不改变先前文件。配额包含已发布但没有日志授权的孤立正文；损坏文件不被跳过来腾出空间。目录扫描还有 `max_items * 2 + 32` 项上限，避免大量遗留文件导致无界遍历。

同目录临时文件写入、flush/fsync 后原子发布，头与正文在一个文件中，因此没有“只发布元数据、正文尚未到位”的有效引用。写入失败只清理本次临时文件，已有内容保持。临时清理失败不掩盖原始故障；临时文件不因重启自动升级成媒体。此处不承诺断电时目录项的持久性，也不是操作系统沙箱。

正文发布后才能写 media_saved。来源日志提交失败时向上传播 StorageError，可能留下占用配额的孤立完整文件，但 `SessionMedia.read` 没有对应日志就拒绝；不重跑原下载、采集或生成动作。读取比较日志与文件中**完整**引用，不只比较正文哈希，所以外部修改 MIME/时间/所有者也不能悄悄成为同一份证据。

路径拒绝目录/文件链接重定向，包括指向另一个会话的链接。主/子普通文件、规则、Skill 与文档工具已保护整个 `_media` 根；显式批准的通用命令仍按当前系统账户权限运行，不受此逻辑目录保护构成的系统沙箱限制。不同主/子消费者须注入独立所属根，不共享未受限的全局文件读取接口。

C00.3 新增 `MediaStore.restore(MediaContent)`：复制可信读取端口已验证的完整证据，保留原 ID/来源/时间，复用配额和原子发布；已有相同内容幂等返回，不覆盖不同证据。`read_frozen(MediaRef)` 只读验证归档副本，不创建目录/锁、不授予活动会话读取权限。模型录制按同名 `.media` 目录冻结副本，详见 [多模态输入](multimodal-input.md)。

## 备份与验收

停止全部写入后备份完整 sessions 目录，必须同时保留 JSONL、`_media` 和已有 `_artifacts` 等子树。恢复到新根后仍按原 ID/版本和完整来源读取；只有 JSONL、缺少正文的备份不能恢复媒体，不能通过重做外部动作自动补造。见 [备份与恢复](install-and-recovery.md)。

业务入口：[test_media_evidence.py](../tests/features/test_media_evidence.py)；契约和文件边界：[test_media_evidence.py](../tests/test_media_evidence.py)。19 项业务场景、51 项低层边界，连同架构共 95 项定向通过；提交门禁执行 Ruff format/lint、Pyright 和全量 pytest。

场景包含有效 2x2 PNG 的 CRC/压缩正文、真实 PCM WAV 的标准库解码、重启和完整目录迁移、文本附件并存、缺失/损坏/元数据篡改、完整字节/数量限制、跨会话复制、并发配额、部分写入/fsync/发布/来源日志失败、临时文件不升级、IO 期间切换所有者，以及真实主/子工具的私有目录拒绝。故障注入分别发生在明确的 IO 或日志提交边界，不用假成功替代文件写入。

这些测试只证明二进制保存和来源边界；没有看图、截图、录音、播放、付费模型调用或真实模型质量评估。C00.3 图片消息编码与媒体摘要重放的工程验收另见 [多模态输入](multimodal-input.md)；音频/视频编码、图片解码与具体能力接入各自单独验收。
