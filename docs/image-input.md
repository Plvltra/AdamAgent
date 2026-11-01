# C04.1 本地图片输入

状态：真实 PNG/JPEG/WebP 解码、版本化媒体及原工具闭环已实现；视觉问答、OCR 与真实识别质量仍分别为 C04.2～4。`load_image` 成功只表示图片可完整解码并已保存，不表示模型看过或理解了图片。

## 配置与使用

先在独立项目环境显式安装解码依赖：

```powershell
uv sync --locked --extra vision
```

在用户配置中启用：

```toml
[vision]
enabled = true
max_bytes = 10485760
max_pixels = 16000000
max_dimension = 8192
```

默认关闭；关闭时不注册工具，也不导入 vision 包/Pillow，不影响普通文字聊天。启用但未安装 Pillow 时，第一次加载返回 `dependency_missing` 和安装提示，不静默改用头部猜测或其他后端。三个限制均为正整数，拒绝布尔值；字节最多 10 MiB（对齐当前媒体冻结上限）、像素最多 64000000、单边最多 16384。默认像素和单边限制更小。

可在聊天中明确请求“仅用 load_image 加载 sample.png，返回引用和尺寸，不要声称已识别内容”。按需工具模式须先发现 vision 分组或 load_image，再在后续请求调用。工具仅接受 `path`；不接受 URL、MIME、自选来源、会话或权限参数。

结果包含：

- `status=loaded` 与完整 `media`，沿用 C00.2 的 schema_version=1、所有者、真实工具来源、UTC 时间、原字节数和 SHA-256。
- `image.format` 为实际 PNG/JPEG/WEBP，不由文件扩展名决定；`width` / `height` 是原始像素尺寸。
- `orientation` 保留 EXIF 1～8；缺失为 null，不伪造为 1。方向 5～8 的 display_width / display_height 换轴，镜像含义仍由原方向码保留。
- `model_input_attached=false`：元数据和引用 JSON 是文字工具结果，不是已附加图片，不产生额外模型请求。

## 显式模型输入

宿主可从当前会话的完整工具结果还原 MediaRef，再调用已有 `AgentApplication.send(question, media=(reference,))`。这个 API 只接受已提交引用，不重新加载路径、不切换会话；应保留确切引用而不是仅凭摘要猜测另一个来源。见 [多模态输入](multimodal-input.md)。

必须先给实际模型适配器显式声明对应 MIME 能力并注入当前会话媒体读取端口。默认 DeepSeek 仍是纯文本；加载图片不会修改 llm.toml 或升级模型。无能力时 send 在登记新轮次/HTTP 前明确拒绝，不把附件删掉改发文字。当前终端没有隐式“上传并看图”命令；C04.2 另行接真实视觉后端和受控问答入口，本阶段不宣称已完成。

程序化验收在加载后显式附图，并解开 MockTransport 实际 data URL 比较完整原字节。它证明本地传输边界，不证明真实视觉模型效果，也不做图片内容判读。

## 解码、授权与失败

[ImageTools](../adam/tool_calls/vision_tools.py) 沿用原发现、模式、路径规则、Hook、工具预算、取消及执行日志。能力为 SESSION_STATE：plan 可调查并保存内部证据，不授予编辑用户文件或执行程序权限。只有原执行器绑定的当前活动工具来源能发布，直接调用或伪造来源均拒绝。

PathPolicy 继续限制读取根、凭据和保留目录；额外写权限不隐含读权限。只接受普通文件，读取最多 max_bytes+1，再检查路径与取消；之后只使用这份不可变快照。与其他本地读取工具一样，这不是对敌对本机并发写进程的 OS 沙箱；路径检查不能消灭所有检查/使用竞态。源文件可在成功后变更或删除，旧媒体版本不跟着改变。

[ImageLoader](../adam/vision/service.py) 只消费解码和发布窄端口，不依赖路径、配置、SessionStore 或应用内核。[PillowDecoder](../adam/vision/adapters/pillow.py) 只在调用时导入 SDK，依次检查实际头部的尺寸/像素、单帧、verify，再重新打开实际 load 并取 EXIF。只识别出头部不是成功。临时流与图像都显式关闭；不修改 Pillow 全局像素阈值或截断容忍开关。若外部代码已启用 LOAD_TRUNCATED_IMAGES，返回 unsafe_decoder_settings，不把宽松解码当完整验证。

| 分类 | 结果 |
| --- | --- |
| byte_limit / pixel_limit / dimension_limit | 在相应资源边界停止，不保存截断或缩小替代图片 |
| animated_image | APNG/动画 WebP 拒绝，不悄悄只取第一帧 |
| unsupported_or_corrupt | 不是可识别的单帧 PNG/JPEG/WebP，或头部已坏；不假装能区分所有未知格式与坏文件 |
| corrupt_image | 完整性、像素、CRC 或 EXIF 方向损坏，提示不含原文件/解码器正文 |
| output_limit | 来源实际 JSON 编码长度加固定元数据预留放不进结果上限，解码/发布前停止，避免保存后丢掉完整引用 |
| 取消 | 阶段边界检查；发布前取消不保存，原生解码期间不能强制中断 |
| 正文或来源保存失败 | 停止本轮，不交付引用、不自动重试；来源失败可能留下无授权孤立正文，占用配额但不能被读取 |

字节/像素边界不是解码进程内存硬上限，也不是原生库漏洞防护沙箱。原字节、EXIF 及可能的附加数据均保持不变，不旋转重编码、不去除隐私元数据；显式附图会发送这份完整证据，需由用户决定材料是否适合提交。

## 保存、子任务与回放

正文沿用 `_media`；JSONL 新增的只有原 `media_saved` 完整引用，格式未另造。尺寸/方向随对应 `tool_finished` 结果保存。主子分别持有自己的 store 与读取授权，子媒体位于 `_media/children/<parent_id>/<child_id>`；不把主引用自动共享给子任务，也不因子结果提到 ID 就授予父读取权。

重启不重新读取图片、解码或发布孤立正文。备份保持整个媒体目录及来源会话日志；删除原图片不影响已提交证据。

仅加载的录制可以离线重放工具事实，不再次读源文件/解码/联网。**事实重放不等于图片二进制已冻结。** 只有后续真正作为模型输入的图片才按 C00.3 冻结并校验同名 .media 副本；附图回放仍须通过摘要/完整来源验证，见 [录制与重放](turn-recording.md)。

## 验收与限制

[29 项业务场景](../tests/features/test_image_input.py)、[36 项解码边界](../tests/test_image_decoding.py) 和 [13 项配置/独立进程导入边界](../tests/test_vision_config.py) 共 78 项新增通过；加架构约束共 105 项定向通过，与媒体/录制分享/子任务提案/发现/Hook/规则共 409 项联合回归通过。正常路径经过真实本地文件、Pillow、原工具/JSONL、媒体保存、重启与显式模型输入；故障明确注入解码返回后的取消、正文原子发布和来源日志提交边界。覆盖按需发现/原规则、Hook/预算、父子隔离、伪造来源、完整字节保真与离线事实重放。

真实解码后端为本机独立环境的 Pillow 13.0.0，样本由编码器程序化生成，只作字节/元数据断言。未产生或查看截图、未看图、未调用付费模型。JPEG/WebP 的有损像素和非文字布局质量不是本阶段判据；OCR、扫描 PDF、视觉推理与 C04.4 的真实对照仍未验收。提交门禁执行 Ruff format/lint、Pyright 和全量 pytest。
