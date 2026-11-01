# C04.2 显式本地视觉问答

状态：单图问题、真实本地模型、原图引用及统一请求/轮次记录已接通。两批固定 4 题请求与保存均 4/4 成功，严格语义分别 2/4、3/4；这不是可靠的通用布局/OCR 能力，不能据成功响应自动执行动作。C04.3 OCR/扫描页及 C04.4 综合质量对照仍待实现。

## 使用边界

- 先用 [C04.1 load_image](image-input.md) 在主会话真实加载一张获准图片，保留完整 `media_id`、`version` 和所属会话 ID。问答只消费已解码的 load_image 工具来源；不接受 URL、路径、自报 MIME、跨会话引用或其他未经解码的二进制来源。
- 退出原聊天或关闭宿主以释放会话写锁，再显式打开问答入口。不会自动上传图片、启动服务器、下载模型、读取默认 llm.toml 密钥、改写默认 DeepSeek，或把文字客户端升级为视觉模型。
- 保留当前会话原系统约束，附加视觉资料不可信和不确定性提示；只发送当前问题和这一张原图。不带旧聊天、旧工具、记忆或新工具定义，不允许执行模型返回的工具请求。
- 返回 `ImageAnswer` 包含已提交正文、完整原图引用及 session/turn/request ID。`images` 是宿主对输入来源的绑定，不是模型框选、语义正确或精确定位的证明。
- 发送原始完整字节，包括 EXIF/附加信息，不自动旋转、缩小或剥离元数据。用户应先决定图片内容与隐私元数据是否适合交给所选模型。

## 本地后端

首个真实后端是 [llama.cpp 多模态 Chat Completions](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)；使用官方 [Qwen2.5-VL-3B-Instruct-GGUF](https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF) 的 Q4_K_M 语言模型及 Q8_0 视觉 projector，模型许可证 Apache-2.0。没有把 OCR 服务或固定字符串替身当视觉模型。

本次固定运行时为 llama.cpp b12032、commit dde879bc4，Windows x64 Vulkan 包；模型 revision `5037fcf163dd95d1e41d1974465f0898ed108ca2`。下载文件和 SHA256 见 [原始验收证据](evaluations/c042-visual-first.json)，约 2.8 GB，模型/二进制不提交仓库。正常启动/聊天/pytest 不会下载或运行它们。

安装好所选运行时和模型后，由用户显式启动服务，例如本次参数：

```powershell
& .\llama-server.exe `
  --model .\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf `
  --mmproj .\mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf `
  --alias adam-vision --host 127.0.0.1 --port 8087 `
  --ctx-size 16384 --parallel 1 --n-gpu-layers 99 --threads 8 --no-webui
```

本次机器为 RTX 3060 12 GiB、i9-10900F、64 GiB RAM。上面不是任意机器的内存/延迟保证。服务器仅绑定回环、无 Web UI；本适配器不管理服务器生命周期或认证，固定 `local-unused` Authorization 不是安全密钥，不能把此服务暴露到公网。

`LocalVisionConfig` 只允许 `http://127.0.0.1:1024..65535/v1` 字面地址、确切 ASCII 模型别名和 (0,600] 秒 IO 超时。禁用环境代理、重定向和自动重试；不接受 localhost/DNS、远端地址、URL 凭据或查询参数。宿主必须确认已加载真正匹配的视觉模型和 projector；别名和 `/models` 自报不能证明权重或效果。

## 显式命令与宿主 API

```powershell
python -m adam.cli.image_question `
  --sessions-dir <原主会话目录> --session <8位会话ID> `
  --media-id <load_image返回的完整ID> --version <64位SHA256> `
  --model adam-vision --endpoint http://127.0.0.1:8087/v1 `
  --question "Which object is inside the outlined region?"
```

缺省 SSE，`--no-stream` 可显式选择完整 JSON；协议不匹配时失败，不自动重发为另一协议。成功输出 JSON 正文及完整引用，失败返回非零；最终保存失败会给 `partial_reply`，但不声称回答已提交。Ctrl+C/取消返回 130。

宿主也可使用 `bootstrap.open_image_questions(sessions_dir, session_id, local, limits=..., context=..., cancel=...)`，并调用 `ask(question, reference)` 或 `ask_saved(question, media_id, version)`。工厂独占会话写锁和自己的可取消 HTTP 连接，退出/异常都释放。不与正在运行的主应用共享存储或连接。

CLI 使用 `AgentConfig` / `ContextConfig` 默认额度：会话 50000 token，输出 4096，输入 32768、余量 2048；每次问图最多一个物理请求。它不读取主配置；项目如有更严格额度，应由宿主显式传入原项目配置。已有主日志及子会话的已知/未知消费仍会恢复，不能通过换入口清零。首版面向主会话保存的 load_image 引用，不提供子会话目录发现或跨父子图片共享。

## 统一证据、费用与失败

[ImageQuestions](../adam/application/image_questions.py) 使用原 ContextBuilder、RequestRunner、SessionAccount/SharedTokenBudget 和 SessionStore；不自建独立模型消费文件。`restore_account` 在持有主会话写锁后逐个取得既有子日志写锁、恢复其权威消费快照，不修改委派索引或重发任务。缺失/损坏/占用不能按零处理。本入口不派生新子任务，整个调用期间复用同一共享账本。

在 `request_started.context.image_question` 保存完整输入引用、后端标识和 `cost=null / currency=null / cost_status=unpriced`；它是来源/无价格证据，不是已计费图片量。`contracts.vision` 校验这些引用必须与本轮一张原图完全一致，不能伪造价格为零。原 `request_usage` 保存实际 token 或未知，`request_finished` 保存耗时与成功/超时/取消原因，轮次独立提交。旧文字请求没有新字段，原 JSONL 版本不变。

本地算力无可核实单价，费用保持未知；不声称本机用电/折旧零成本。每图 8192 token 仍只是 C00.3 的保守本地预留，不是图片 tokenizer、价格或服务端实际消费硬限制。图片计费、音频时长、独立调用数和可选价格的完整多维结算仍为 C00.4，未提前勾选。

- 不支持/引用变化/来源撤销/预先取消、输入或共享额度预留不足：登记新输入及 HTTP 前拒绝，不降级成纯文字。
- 请求开始先可靠落盘，再调用一次模型；连接失败、超时、协议错误、缺少可靠用量或取消都不自动重问。已发送但消费未知会阻止本组继续；明确未发送才结算零。
- 真实消费超额仍先留证，再拒用回答。预留不能强制服务端计费不超限。
- 已获得有效响应但缺少用量：物理请求成功，整轮失败且消费未知。模型返回工具请求/空文本也不执行或假装可用。
- 请求结算或最终保存失败：停止且不重问；请求成功不等于回答提交，最终保存错误携带生成正文但成功历史不增加。
- 取消关闭当前客户端 IO，不证明服务器 GPU 计算已停止；IO 超时也不是完整推理墙钟硬截止。崩溃遗留由原恢复逻辑收尾，不自动续发。

专用问答是显式宿主/CLI 流程，未接入普通聊天的工具选择，也未接入单轮录制 CLI 的配置构造。统一会话记录和媒体来源不等于该独立流程已做冻结离线重放；C00.3 对普通 `send(media=...)` 的录制契约保持不变。

## 真实固定验收

可复现入口是 [scripts/vision_acceptance.py](../scripts/vision_acceptance.py)，仅用于仓库开发验收。Pillow 生成固定 800x600 合成 PNG，不是截图；`generate` 拒绝已有批次目录。模型问题/预期和图 SHA 在推理前固定；加载阶段使用测试 fixture 的 MockTransport 只编排真实 load_image，正式问答必须经本地真实 SSE，不把加载模拟用量计入真实成本。

```powershell
python -m scripts.vision_acceptance generate --output .cache/vision-new-batch
# 按视觉任务规则由独立视觉子代理核图；主代理不读图。
python -m scripts.vision_acceptance run --output .cache/vision-new-batch `
  --manifest <下载校验清单.json> --process-record <实际进程记录.json>
```

`run` 不启动或调参服务器；失败停止整批，不自动换会话继续消费。未传运行时/下载清单时元数据保持未知，不能据 alias 推断实际权重；语义对错须单独人工判读，脚本的成功退出仅表示请求与保存。不同字体/Pillow/服务器版本、采样默认值可能影响结果，不承诺字节或回答跨机器完全一致。

首批实际结果由独立视觉子代理核图/判读，主代理只读文字报告。完整原始回答、预期、逐题引用/请求事件与失败说明在 [首批证据](evaluations/c042-visual-first.json)。

| 问题 | 严格语义 | 实际输入/输出 token | 请求毫秒 |
| --- | --- | --- | --- |
| labels_words | 通过：ORBIT/HARBOR 对应正确 | 709 / 30 | 41094 |
| labels_geometry | 失败：形状和左右正确，误说标签在下方 | 719 / 37 | 1875 |
| layout_a_relations | 通过：框内、上下、框外右侧均正确 | 720 / 38 | 937 |
| layout_b_relations | 失败：误认红方在框内、遗漏绿三角及所问上下关系 | 720 / 36 | 922 |

合计 3009 实际 token，44828 请求毫秒；首题冷处理与后三题不能直接作稳定性能比较。互换图左右答案随图变化，但包含和框内上下仍错，不能算完整互换布局通过。日志建议 Qwen-VL grounding 至少 1024 image tokens；本次未设置该参数，保留警告而不认定其为失败原因，也不重试/调图来覆盖失败。

### 最终共享额度集成复验

在补齐既有主/子共享额度恢复后，使用正式脚本重新生成独立批次；新的独立视觉子代理在推理前核图并逐题判读。三张图片 SHA256、字体与四题问题/预期均与首批一致，不覆盖首批证据。原始回答、逐题预期、请求 ID、用量及同图核对见 [最终复验证据](evaluations/c042-visual-final.json)。

| 问题 | 严格语义 | 实际输入/输出 token | 请求毫秒 |
| --- | --- | --- | --- |
| labels_words | 通过：ORBIT/HARBOR 对应正确 | 709 / 26 | 313 |
| labels_geometry | 通过：形状、左右和标签在上方均正确 | 719 / 49 | 516 |
| layout_a_relations | 通过：框内、上下、框外右侧均正确 | 720 / 41 | 437 |
| layout_b_relations | 失败：仍误说红方在框内，遗漏蓝圆及绿在蓝上的关系；左侧判断正确 | 720 / 38 | 437 |

最终请求/保存 4/4，严格语义 3/4，互换布局仍未整体通过。合计输入 2868、输出 154、总计 3022 token；请求耗时合计 1703 毫秒，问答墙钟合计 1939 毫秒。服务报告 2864 输入 token 命中缓存，不从总用量扣除；这是热缓存批次，不能与首批冷请求直接比较。费用及币种保持未知而非零。

服务器和采样默认值未改；两批回答有差异不证明共享额度恢复提高了模型质量。没有重试、调题、换图、换模型或 OCR 替代。复验结束后已核对运行时路径并关闭本次本地服务；模型与二进制仅留在忽略缓存中。
这是首个真实后端的有限烟测，不是 C04.4 清晰/模糊/旋转/混合 PDF 的综合验收，也不是 OCR 字错率/区域定位测试。失败原样保留，不能从工具成功推断业务判断正确。
