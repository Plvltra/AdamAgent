# E2 本地文档检索与 RAG

更新日期：2026-10-29。状态：E2.1～E2.5 工程实现与离线验收完成，真实本地 embedding 对照已执行；真实聊天模型回答质量另列人工验证。主任务：[练手 TODO](agent-practice-todo.md)。

## 已采用的范围

用户授权全部采用推荐，并要求每个子任务验证后独立提交。E2 在独立工作树及 codex/e2-local-rag 分支完成，随后按用户要求将主线整理为 E1 → E2.1～E2.5 → E3.1～E3.4；保留各阶段提交。

指定目录中的 UTF-8 Markdown/TXT；读取权限沿用现有策略。关键词基线先行，随后增加本地 embedding 对照，不运行独立向量数据库。按需工具调用，不对每条聊天强制检索。保留路径、标题、行号、文件版本和片段正文，明确证据不足；文档内容不是新的系统指令。

索引是可重建缓存，独立于 JSONL 会话和跨会话记忆。搜索前检查源文件；更新、删除、读取异常和索引异常不能静默沿用旧资料。文档检索结果计入既有工具和上下文预算。

## 子任务与验收

| 子任务 | 内容 | 状态 |
| --- | --- | --- |
| E2.1 | 固定文档/问题、分块及关键词搜索基线 | 已完成：6 个业务用例；提交时执行全量检查 |
| E2.2 | 来源、版本、持久索引、刷新和故障保护 | 已完成：真实文件、重启、双进程及故障验收 |
| E2.3 | 工具闭环、配置、引用依据及上下文预算 | 已完成：假 HTTP + 真实检索及日志、容量与配置验收 |
| E2.4 | 本地 embedding、同题对比与独立模型缓存 | 已完成：本地真实模型对照、向量缓存及失败测试 |
| E2.5 | 整体验收、效果报告与操作文档 | 已完成：44 项 E2 场景、子任务/取消/权限/故障和无 embedding 依赖安装验证 |

## E2.5 验收与使用边界

- E2 的六个业务测试文件合计 44 个场景通过；E2 独立验收时整个项目收集 546 项，提交前执行 Ruff 格式/lint、Pyright 和全量 pytest。
- 真实双进程刷新、子 Agent 经工具读取资料、主 Agent 收集结果、相对 workspace 和文档配置路径都经过实际业务流程。
- 缓存不能绕过撤销后的源读取权限；文档及向量保存失败保护旧文件，并明确查询失败。取消在扫描文件、向量片段及提交边界传播，不能把部分向量当成完成索引。
- 默认依赖在独立 Python 3.11.9 环境安装成功，未安装 FastEmbed；聊天 --help 和关键词查询可用，语义查询明确以退出码 1 提示安装可选依赖。
- 非预期的无答案、同义表达和语义误召回保留在报告中；真实聊天模型的忠实性没有用假模型冒充验收。

`tests/features/test_document_retrieval.py`：关键词及分块；`test_document_index.py`：刷新和持久化；`test_document_tools.py`：模型工具闭环；`test_document_embeddings.py`：向量及模型加载边界；`test_document_evaluation.py`：指标和独立入口；`test_document_acceptance.py`：跨组件边界。

## 日常操作与恢复

1. 默认从关键词开始，在主配置中设置 [documents].roots。只查询明确加入的目录；不用给整个硬盘建索引。
2. 文件新增、修改、删除无需手动刷新，下一次查询重新扫描并更新。来源版本指向本次观察到的文件内容；已有会话中的证据快照不会随源文件修改。
3. 读取错误先修正编码、路径或权限；资源上限提示先缩小目录。失败不会被包装成正常无匹配。
4. 缓存损坏时停止访问该缓存的进程，把对应 JSON 移到备份位置后再查；不要删除源文档或 sessions。文本和 .vectors.json 均可重建，models 子目录是可重新下载的模型资产。
5. 语义模式加载失败时显式安装依赖并 prepare-model。不会自动联网，也不会悄悄改为关键词；可以由用户明确把 method 改回 keyword。

扫描会读取指定语料的当前字节来判断变化，这是小规模、可解释的练习方案；它不是百万文档索引服务。原生文件读取及单次 ONNX 推理不能在执行中途强杀，取消在边界生效。仅支持 Markdown/TXT，不包含 PDF/OCR、网页抓取、重排器或自动语义冲突裁决。

## 提交与主线集成

E2 原开发目录为 G:/Agent/adam-e2，分支 codex/e2-local-rag，基线 0cbb0f8。五个 E2 提交原样保留，四个 E3 提交接在其后，作者均为 Plvltra。集成主目录为 G:/Myself/Adam；原 main 保存在 backup/main-before-e2-e3-20261029。

集成在独立工作树完成，bootstrap 同时注册检索与计划工具，配置同时接受 documents 与 planning。冲突逐项合并，保留原有注释及双方架构图；验收通过后才更新主线。新增业务测试验证检索结果成为计划证据并可重启读取，合计收集 579 项测试，提交检查执行全量回归。缓存、模型和虚拟环境不提交 Git。

## E2.4 本地语义检索

安装可选依赖 `uv sync --locked --extra documents`，显式运行 `python -m adam.document_search prepare-model` 准备默认模型，再把 [documents].method 改为 "semantic"。默认模型 BAAI/bge-small-zh-v1.5 在 CPU 本地运行，正常检索使用 local_files_only，不上传文档，不消耗聊天 API 额度。安装及模型准备需要网络；默认关键词模式不需要这些依赖。独立命令不加载 llm.toml。

向量按片段 ID 缓存，模型名称、FastEmbed 版本或编码方式变化会重建；源文件版本变化只生成新片段向量。查询和文档使用各自编码接口，长内容按 256 字符窗口汇总，避免静默丢掉片段尾部。分数为余弦相似度，semantic_min_score 默认 0.5，可调整但不能当事实置信度。更换同名模型权重后应清理向量缓存。向量缓存与文本索引共用 snapshots 的校验和、文件锁调用边界及原子保存协议；损坏和推理失败不静默回退关键词。

独立命令示例（公共参数放在子命令前）：

```powershell
python -m adam.document_search --root docs search "会话恢复策略"
python -m adam.document_search --cache-dir .cache/e2 prepare-model
python -m adam.document_search --root tests/fixtures/documents --cache-dir .cache/e2 --semantic --output .cache/e2/report.json evaluate tests/fixtures/document_questions.json
```

若 [storage].documents_dir 使用自定义缓存，prepare-model 的 --cache-dir 应与之相同。--root 可重复；默认相对 --workspace，外部目录还需 --allow-read-dir。CLI --semantic 启用语义搜索或双策略评估；不读取或修改聊天配置。

真实对照及局限见 [评估报告](document-retrieval-evaluation.md)。实现依据：[FastEmbed 模型及 CPU 支持](https://qdrant.tech/documentation/embeddings/fastembed/)、[本地缓存加载](https://qdrant.tech/documentation/edge/edge-fastembed-embeddings/)。

## E2.3 使用方式与工具闭环

在 config.toml 的 [documents] 设置 roots = ["docs"] 即启用 search_documents；默认空数组保持原有工具集合。源路径相对 --workspace，缓存 storage.documents_dir 相对主配置（默认 ADAM_HOME/documents）。工作目录外的文档仍需 permissions.additional_read_dirs 授权，添加 roots 不等于授予权限。普通聊天不扫描文件。

模型按需传入 query 和可选 limit（1～20）。工具返回路径、行号、标题、完整片段、文件 SHA-256、片段 ID 和索引代次；结果落入现有工具消息与 JSONL。模型说明要求引用依据并说明证据不足，工具返回参考资料而非系统指令。no_match、output_limit 和 ok=false 分别表示没有命中、结果容量不足、执行失败。结果受 max_result_chars 限制，只省略整个片段；后续请求沿用 V3 的总上下文预算。

子 Agent 使用相同配置源，但逐个检查自身权限；不能直接读文档缓存绕过源文件授权。搜索不会写回源文档。测试检查真实模型请求内的材料和持久日志；预设模型回答只验证引用传递，不宣称真实模型一定忠于证据。

## E2.2 索引与刷新

DocumentSource 通过注入的 PathPolicy 检查范围，跳过隐藏项、链接及缓存目录，仅读取 MD/TXT。每次扫描按字节计算 SHA-256，因此文件大小和 mtime 不变的修改也能识别。文件数量、字节数、扫描条目、时间和片段数都有上限；超限及读取失败不返回不完整结果。

DocumentIndex 按工作目录、文档根及分块设置隔离缓存；持文件锁重读旧索引，仅重分块变化的文件，删除失效来源。JSON 校验和保护缓存完整性；更新使用临时文件、fsync 和原子替换。失败保留旧文件，但本次查询明确失败，不能用旧内容冒充最新内容。损坏缓存保留以便排查，移走后可从源文档重建。单次扫描提供观察到的文件版本，不承诺整个文件系统的原子快照。

## E2.1 基线

六篇人工编写资料和 16 道固定问题在 tests/fixtures/documents 与 tests/fixtures/document_questions.json。资料中的重试次数、缓存时间均为教学数据，不是 Adam 的实际配置。题目覆盖原词、有答案但措辞不同、session 的多义、无答案和删除资料；真实文件修改/删除在 E2.2 验证。

按标题、段落和最大字符数分块，英文单词/标识符及中文双字切分，使用 BM25 排序。结果只有正得分候选；分数不表示事实置信度。片段 ID 绑定源文件版本、位置和内容。

自动化使用真实检索和临时文档；模型闭环使用假 HTTP。真实检索质量可以独立测量，假模型回答不能证明真实 LLM 忠于证据，后者留人工核查记录。
