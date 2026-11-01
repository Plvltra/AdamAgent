# 多模态输入契约（C00.3）

最近核对：2026-11-02。已实现公共消息/能力视图、有声明的图片编码边界和媒体冻结回放；不是图片加载/解码、OCR 或真实视觉质量验收。C04.1 文件选择/有界解码已实现，C04.2 提供 [显式本地单图问答](visual-question-answering.md)，真实烟测保留失败，不等于通用视觉质量达标。已配置的 DeepSeek 客户端默认保持纯文本，不因模型名字或用户附件自动升级。

## 宿主入口与来源

`AgentApplication.send(text, media=(reference, ...))` 接受当前已恢复会话的已有完整 `MediaRef`，最多 16 份，保留顺序。它不接受路径、外部 URL、图片正文或供应商消息；不创建新上传轮次、不隐式切换到引用的所属会话。媒体可以来自该会话先前已保存的用户输入或工具；建立证据的宿主继续遵守 [C00.2 来源契约](media-evidence.md)。

`MediaInputService` 先检查确切模型声明、数量和总字节，再通过所属 `SessionMedia` 读字节并比较完整来源。在登记新轮次前完成；不支持、缺失、损坏、来源替换或 IO 期间切换所有者时明确失败，不丢图改发文字。主/子分别注入所属存储；子根为 `_media/children/<parent_id>`，父附件不会随文本任务自动继承。普通工具继续保护整个 `_media`。

## 公共消息与模型适配

[contracts.model_input](../adam/contracts/model_input.py) 是 B24.1 与 C00.3 共用的必要增量，不依赖 SDK 或 IO：

- 原 `content` 保持文字；只有 user 可增加非空 `media` 引用列表。`Message.parts` / `MessageView.parts` 返回文字在前、媒体按顺序的 `TextPart` / `MediaPart`；不支持任意图文交错。
- 工具调用 ID、名称、原始参数、工具结果 ID 及 `reasoning_content` 逐值保留；未知供应商字段明确拒绝，不把不可移植字段静默删掉。
- `ModelCapabilities` 绑定确切模型及 MIME、单次请求的媒体总数/总字节、工具、SSE 和推理字段能力；旧客户端没有声明时视为纯文本。
- 当前图片编码支持显式声明的 `image/png` / `image/jpeg` / `image/webp`；默认最多 4 份、10 MiB（整个请求，含历史）。音频/视频等输入需对应消费者扩展适配，当前不会被伪装成图片。

[ChatInputEncoder](../adam/llm/message_encoding.py) 由宿主显式注入 `LLMClient`，接收能力声明与窄媒体读取回调。只有发 HTTP 之前才将固定字节转换为 Chat Completions 的 `image_url` data URL；文字、工具、推理字段和原消息副本不变。读取回调负责当前会话授权，编码器再次核对完整引用、大小和 SHA-256；它不解码图片，不把 MIME 声明当成格式验证。

普通聊天的媒体契约不自动增加模型连接或密钥配置；C04.1 的 [vision] 只启用解码，C04.2 的独立显式回环后端不升级默认聊天。要测试有媒体能力的适配器，参考 [业务夹具](../tests/features/test_multimodal_input.py) 的显式宿主组装；其中模型 HTTP 是 MockTransport，不表示现有远端供应商支持视觉。B24.2/3 的第二协议、切换点与跨协议验收仍未完成。

本地格式/能力/发送前版本失败使用 `model_input_invalid`，用量结算为 `(0, 0)`，不会自动重试；输入阶段拒绝则连轮次/请求都不创建。已有历史含媒体而后续改成未声明模型，也不能静默去图后发文字。

## 持久化、预算与压缩

`turn_started.media` 保存原始输入的完整引用；成功 user 消息与任务导向摘要的当前问题必须使用同一组引用，不得改来源、换版本或遗漏附件。会话 JSONL、录制模型输入和上下文前缀观测均保留引用，不写 base64、文件路径或媒体字节。旧文本会话的事件/预算不变。

每个实际附加媒体的消息在 JSON 估算之外另预留 8192 token。它是本地保守预留，不是图片 tokenizer、价格保证或非 token 计量；真实 token 消费仍采用远端 usage，未知保持未知。C00.4 已另接可选多单位计量，见后文。

历史压缩与专门反思若引用源问题中的媒体，会同时附上真实媒体供模型读取，而非只发送引用 JSON。摘要对相同完整引用去重；反思同样计入媒体预算，默认 4096 输入预算放不下图片预留时明确记录 `input_budget` 跳过，保留普通回答机会。摘要失败保留原历史，不用文字替代图片来强行压缩。

## 媒体冻结与离线回放

录制带媒体的请求须给 `TurnRecorder(media_reader=...)` 提供当前会话授权读取端口。显式 `send(..., media=...)` 在登记本轮前冻结；历史、摘要及其他模型请求的媒体在物理调用前再次核对并冻结。无读取端口或归档失败即停止，不请求远端。现有开发录制入口注入所属 SessionMedia，普通启动仍不自动录制。

冻结目录固定为 `<recording.jsonl>.media/<session_id>/<media_id>.media`，不从 JSON 中读取路径。`MediaStore.restore` 保留原身份、时间、来源与字节，持锁执行原配额和原子发布；已存在的同一证据幂等复用，不覆盖不同证据。默认归档配额独立为单份 10 MiB、每会话 64 MiB、128 份，不包含在 JSONL 的 32 MiB 限制里。

JSONL 新增可选 `context/input_media` 观察，原 runtime 起点保持不变。`TraceState` 将显式输入及模型请求媒体与已采集的会话来源逐项关联。`read_recording` 先限制媒体数量、单份及累计字节，再只读核对冻结文件的完整引用和实际字节摘要，不建目录/锁、不访问原会话或原媒体。副本缺失、损坏或来源变化时拒绝，不能返回 matched。旧纯文本录制不需要旁路目录。

离线运行使用已验证引用和原冻结响应，运行生产 Conversation/预算/日志比较，不调用模型、网络、原文件或原外部动作。即使原会话整个目录已删除，仍可回放。分享/备份/改名时须将 JSONL 与同名 `.media` 目录一起处理；该目录包含真实媒体内容，不是脱敏后的安全摘要。详见 [录制回放](turn-recording.md)。

主线 B13.1 的 `parse_recording(raw)` 仅验证同一份 JSONL 字节的结构/来源，不访问媒体；带媒体时 `media_verified=False`，纯文本没有待验证副本，保持原解析/读取结果相等。人工分享导出不复制媒体正文，也不因 JSONL complete 而证明副本完整。`read_recording(path)` 另外验证副本后才标记 `media_verified=True`；带媒体但尚未验证的解析结果在 `build_replay` 与媒体输入端口均被拒绝。

## C04.1 本地图片消费者

[图片输入](image-input.md) 已用 load_image 接入真实单帧 PNG/JPEG/WebP 解码、尺寸/EXIF 方向与原字节版本。只返回工具引用并不把图片附到模型请求；宿主后续显式 send(media=...) 仍经过本文原能力/所有者/正文检查，默认文字模型仍拒绝。仅加载的工具事实重放不等于媒体二进制冻结；真正附图后才按本文的媒体请求捕获与验证规则处理。

同步后的 C00.3 独立提交全量门禁 2757 项通过；随后 rebase 到 main 已提交的 808dfb9，B13.2/3 分享与媒体/录制边界联合 192 项通过。真实视觉效果不在这些结果内。

## 验收范围

[35 项业务场景](../tests/features/test_multimodal_input.py) 验证有效 PNG 的实际 HTTP 编码、工具/推理身份、SSE、持久化/重启、无能力与限额、损坏与跨会话/实际子任务隔离、读期间切换、请求前版本变化、预算/保存/取消、摘要/反思、原会话删除后的零网络回放，以及冻结发布/缺失/篡改失败。故障注入点在明确的正文读取、请求登记、原子发布或最终消息提交边界。

[43 项公共/编码边界](../tests/test_model_input.py) 和 [10 项归档边界](../tests/test_media_archive.py) 验证字段拒绝、媒体摘要/来源、显式能力和配额、幂等及只读行为；新增共 88 项通过。与旧文本/工具/SSE/摘要/反思/录制及架构联合回归共 372 项通过。独立提交门禁执行 Ruff format/lint、Pyright 和全量 pytest。没有看图、截图或付费模型调用，不能据此宣称真实图片理解效果通过。

同步固定主线 1b24093 时补 2 项有/无媒体副本场景，验证纯字节解析可用于人工分享但不可绕过实际媒体验证进入回放；原 86 项及 rebase 前 372 项联合验收证据保留。同步后 Ruff format/lint、Pyright 及 392 项联合回归通过，覆盖原文本/重放/流式/摘要/反思及新知识入口/强杀恢复/人工分享；全量 pytest 由本次独立提交门禁再次执行。


## C00.4 多单位消费

2026-11-02：现有图片/token 消费者接入和工程验收已完成；新增 105 项覆盖单位/价格/存储/主子/请求/重放。全量预检 3190 项通过后补收尾竞争观察，94 项相关复验通过；提交门禁继续执行 Ruff format/lint、Pyright 与全量 pytest。注释审查保留原并发、来源、失败和恢复约束。

主配置可选增加 `[consumption]`，缺省 `enabled=false` 完全保留旧 token 流程。显式 `enabled=true` 后，`max_images`（图片份数）、`max_audio_ms`（毫秒）、`max_calls`（物理模型适配器尝试）各自独立；未设置为不限，0 是不允许消费该单位。token 继续使用原 `agent.max_session_tokens`，不把毫秒硬换 token。禁用时配置上限或单价会报错，不静默忽略。

调用范围是同一主会话及所有子会话的聊天/反思/摘要和显式图片问答。主子消费从原已持锁日志恢复；同一成员累计值只替换，不重复累加。每次请求按实际选中的消息计图片，历史同图再次发送仍再计；摘要先去重后的实际图片才计。load_image、网页 HTTP、工具次数不是这里的模型调用数。单独知识审查/评估运行保持它们原独立预算，不混算进会话额度。

```toml
[consumption]
enabled = true
max_images = 20
max_calls = 30
max_audio_ms = 0

# 仅示意宿主自定价格，不是供应商报价；单位是一 token、一图片、一毫秒、一次尝试。
[consumption.prices."exact-host-model-name"]
currency = "USD"
[consumption.prices."exact-host-model-name".per_unit]
input_tokens = "0.000001"
output_tokens = "0.000002"
images = "0.10"
calls = "0"
```

价格只精确匹配模型名，值必须是非负十进制字符串，不接受浮点/指数/NaN。未配置价格不是免费；缺某单位价格或数量未知时只给已知估价小计、总价未知。即使该单位价格为 0，数量未知仍未知。币种不转换或相加，`estimated` 是按配置计算，不是供应商账单。每次开始事件冻结单价，结算与原 token/冻结单价复算一致才接纳。

原 `request_started/request_usage` 和 `compaction_started/compaction_usage` 新增可选 consumption 字段，不增加消费数据库或第二份用量文件。只读 `SessionStore.consumption_usage/report` 与 `python -m adam.session.diagnostics <jsonl>` 可读单位小计、未知单位、分币种估价及未配置价格的请求数。旧 token 输出不改。

明确连接未建立或本地发送前输入校验失败，提交图片/音频为 0，但保留一次适配器尝试。成功响应缺 token 时保留实际提交图片数，token 未知仍停止；不明发送失败/取消保留活跃媒体未知，未使用媒体维度可为 0。进程遗留的未结算请求全部单位未知，恢复不重发。保存失败不把消费抹零；重复/伪造结算拒绝整个追加块。预留只是防止并发重复使用本地剩余额度，不是服务端执行/收费的硬上限。

旧日志没有媒体计量时保留图片/音频未知，启用后会停止新增请求；不回填免费或凭文字猜零。需要新会话或保留旧配置，不提供自动迁移。停用或提高配置是宿主显式决策，不是不可绕过的计费安全边界。主配置不自动被独立图问 CLI 读取；使用 `--consumption-config <file>` 传入只含 `[consumption]` 的独立 TOML，或给 `open_image_questions(..., consumption=...)` 传相同项目配置。此入口不读取默认聊天密钥。

录制启用时在运行起点保存可选配置、每次预算决策保存其他成员消费与在途单位预留，请求收尾也观察最新成员状态，不能拿请求开始时的旧状态冒充结算时状态。重放核对当前主日志的单位快照、重算当前请求预留并执行原预算，不以旧成功记录授权。旧录制没有该标记时保留旧行为；独立图问仍未接入录制 CLI，不把其普通会话日志当作冻结回放。

音频目前只有明确毫秒数量的契约/存储/预算边界，未接可信时长读取、录音或语音模型；未来音频缺时长时不得按 0 准入。业务测试入口：[实际请求/主子/反思/摘要/重放](../tests/features/test_consumption_loop.py)、[存储/估价/故障](../tests/features/test_consumption_evidence.py)、[配置](../tests/test_consumption_config.py)、[单位与并发边界](../tests/test_consumption.py)、[显式图问](../tests/features/test_image_questions.py)。测试只替换远端 HTTP，未调用付费模型；不证明真实模型质量或计费准确性。
