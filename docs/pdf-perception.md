# B17 带页码的 PDF 文档感知

## B17.1 已实现

[extract_pdf](../adam/documents/pdf.py) 接收已经读取的 PDF 字节，返回原文件 SHA-256、总页数、逐页文本、物理页码、截断情况及下一页位置。物理页从封面开始计1；不根据正文印刷数字猜页码。完整原字节决定版本，即使只读取第二页，封面改动也会改变版本。

B17.1 提供独立提取 API，B17.2.1 已注册下述 read_pdf 工具并接入路径授权和读取快照；B17.2.2 也已将 PDF 接入配置目录的资料索引。extract_pdf 本身仍只接收字节，不打开文件路径。

| 结果 | 含义 |
| --- | --- |
| text | 有可提取文字；可能仍有未理解的图片、表格或缺字 |
| image_only + needs_ocr=true | 没有提取文字但存在图像，需要 OCR；本版没有执行 OCR |
| no_extractable_text | 无可提取文字，也未发现图像；可能为空白或矢量内容，不断言页面没有信息 |
| has_images | 页资源或内容中发现图像，包括嵌套 Form 和内联图像；未解码像素，不保证资源实际可见 |
| layout_verified=false | 文本顺序、表格、图表与视觉版面均未验收 |
| truncated + extracted_chars | 返回的页面文本被截断，字符总数记录本次实际提取长度 |

默认原文件最多8 MiB、总页数512、每次20页、解码内容流2 MiB、单页8000字符、总返回32000字符。调用者可在支持的有限范围内提供 PdfLimits。返回截断文本时不声称完整；next_page 指后续物理页，不补回上一页被截掉的尾部，需要重新选择该页并增加字符预算。总字符预算用完即停止返回后续页。

加密文件即使空密码可打开也明确拒绝；畸形文件、超限、任一页提取失败整次失败，不交付前面几页冒充成功文档。不执行 PDF JavaScript、附件、链接或外部图像解码程序，错误消息不复制第三方异常正文。

## 依赖与资源约束

增加纯 Python 依赖 pypdf，uv.lock 固定6.21.4；没有改变其他锁定包。其公开 Configuration / apply_configuration 按执行上下文设置流解码、数组/字典、页树、过滤器、操作数及 Form 调用限制，退出后还原，不影响并行读取。

这限制了输入、部分解析工作量和返回大小，**不是进程总内存硬上限或操作系统隔离**。取消在页面和文本操作边界检查，第三方前置解析不保证立即响应；聊天工具沿用 scan_timeout_seconds 做协作期限检查，不声称是强制中断；没有启动解析子进程需要清理。

pypdf 按页抽取文本，但扫描页需要另行 OCR；整个页面内容流会参与解析，文件小不代表解码后的工作量小。依据：[官方文字提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[PdfReader 参数](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html)。实现同时核对了锁定版本安装源码中的公开上下文配置接口。

## 验收

[15项业务测试](../tests/features/test_pdf_extraction.py) 使用 [真实 PDF 字节夹具](../tests/features/support/pdf_data.py)，覆盖物理页与印刷页区分、原字节版本、图像/嵌套 Form/内联图像/混合页、页内与总截断、空文件/坏结构/加密、范围与压缩流超限、取消、并行配置隔离及不执行活动内容。连同架构检查共37项通过（0.95秒）。最初静态检查发现 Any 参数违反项目规范，已改为 object/PdfObject；测试夹具弃用接口改为当前公开 add_open_action，未屏蔽警告。

真实中文书稿《深入理解 AI Agent.pdf》也进行了文本提取：原文件11084397字节、313物理页，读取122～124页得到1996/1677/1654字符，均有中文且未截断，下一页125。此次显式提高原文件上限为16 MiB；默认8 MiB会正确拒绝该文件。[元数据证据](evaluations/b17-pdf/book-extraction.json) 只保存版本、页码和统计，没有复制原书正文，也没有声称做过看图验收。

Ruff、Pyright及注释单独审查；主线提交门禁执行全量 pytest。B17.2 路径/工具/页级索引接入已完成，B17.3 组合验收与图片分析扩展仍待完成。


## B17.2.1 模型读取工具

新增 read_pdf(path, start_page=1, page_count=5, expected_version 可选)，注册在 documents 分组，能力为 READ_ONLY。主任务、子任务和 plan 模式都使用原路径策略；发现候选仍不授予权限，按需 Schema 与实际请求名单、Hook、共享额度和日志沿用原工具流程。无需配置 documents.roots 即可明确读取已授权 PDF，普通问题不应调用。

[PdfSnapshot](../adam/documents/pdf_source.py) 先重新授权，再通过同一文件句柄有界读取；比较句柄前后及当前路径的设备/文件身份、大小和修改时间。提取完成再次授权、复查元数据；变化或撤权丢弃该次结果。它不加跨进程文件锁，不能保证发现刻意保留所有元数据的并发写入；SHA 始终对应本次实际读取的原字节。后续调用若携带 expected_version，源内容更新便明确拒绝跨版本续读。

[PdfTools](../adam/tool_calls/pdf_tools.py) 使用 tool_limits.max_pdf_file_bytes（默认8388608，上限33554432）、max_pdf_read_pages（默认20，上限100）；TOML 和直接构造共用校验。还沿用 scan_timeout_seconds 的协作期限、max_result_chars 的最终输出预算。解析器其余复杂度和文本上限保持 B17.1 设置。

最终结果包含 path、version、total_pages、pages、next_page 与 layout_verified；页状态、实际提取字符数和 truncated 都保留。超出预算先省略后续整页并修正 next_page，单页仍过长才缩短其文字并标记；基础来源放不下时失败，不用通用 preview 截断掉 JSON 定位信息。想读取被截掉的同页全文需提高结果限制，next_page 不是该页尾部偏移。

工具的原结果随现有 JSONL 和录制保存。重放使用已捕获页面，不再读取当前 PDF；不能把重放结果当作文件当前版本。

[15项接入场景](../tests/features/test_pdf_tools.py) 通过真实文件/解析/应用组装验证两次同版本续读、物理页落盘、零解析重放、主子/plan 读取、范围外/保留/凭据路径读取前拒绝、额外根授权、读取中修改/解析中修改/撤权、输出分页、参数/文件限额、取消/协作超时、IO 异常与 TOML 生效。两组相关回归162项（30.96秒）和111项（32.24秒）通过；补充 TOML 场景后 PDF/配置62项通过（3.84秒）。Ruff、Pyright、注释和架构同步核对，主线提交门禁运行全量 pytest。远端聊天是 MockTransport，未调用付费 API。


## B17.2.2 页级索引与检索引用

配置 documents.roots 后，DocumentSource 读取 Markdown/TXT 和 PDF。PDF 复用 PdfSnapshot 的授权、有界读取及解析后复查，经 extract_pdf_all 一次打开全部物理页；不会为每页重复打开解析器。任一页错误、文字截断、解析期间修改/撤权或协作期限到达，整次刷新失败且旧索引保留，不返回旧片段冒充最新资料。

[Chunk](../adam/documents/models.py) 的 page 是从1开始的物理页码；start_line/end_line 是该页提取文本的行号，不是书上印刷页码或视觉版面的坐标。每页独立分块，片段 ID 包含页码，所以两页相同文字不会合并为一个身份。来源路径和 SHA-256 仍对应完整原文件。MD/TXT 不增加 page 字段，保留原全文件行号和片段身份。

搜索、知识概览和原文查询、语境生成输入、候选知识引用及评估报告都保留 page。PDF 引用缺页码或错绑页码不能通过来源审计；固定题的 PDF 金标准也须绑定页码，另一页的相同原文不计为命中。来源审计仍只核对出处，不证明模型推论正确。

image_only、no_extractable_text 和含文字但有未识别图片的 unread_images 形成带路径、版本及页码的提示。search_documents、知识目录与引用审计使用 source_coverage 保存总数和最多5条来源，明确是否省略其余提示；即使查询 no_match，也不能把扫描件说成没有答案。版面、图像和 OCR 始终未验证。输出放不下完整基础元数据时明确失败，不截断 JSON 来源字段。

索引沿用 documents.max_file_bytes（默认1 MiB，上限32 MiB）与 max_total_bytes（默认10 MiB）；增加 max_pdf_pages（默认512，上限2048）和 max_pdf_text_chars（默认1000000，上限1000000）。单页提取最多100000字符，解码流仍受2 MiB限制，超限不建立部分索引。较大的书稿需要同时明确提高文件/语料字节额度及必要的 scan_timeout_seconds；这些仍是有界输入和协作检查，不是 OS 硬内存或超时隔离。

解析格式及 PDF 限额参与缓存文件隔离；索引可以重建，知识候选持久目录继续使用原资料范围。纯文本语料的 source_generation 也保持原计算口径，避免仅因新增 PDF 功能就让已有知识变成过时记录。新增或改变 PDF 才会改变对应语料来源版本。旧会话和录制保存当时结果，重放不会重新读取 PDF。

[页级索引业务验收](../tests/features/test_pdf_indexing.py) 使用真实 PDF 文件和解析器，覆盖超过单次读取页数的全文、相同文字不同页、混合扫描件、只扫描页的 no_match 提示、JSONL 与无源文件重放、知识目录/语境/候选、错绑页码、同大小同mtime修改、删除、加密/损坏/页数/字符限额、撤权/取消/协作期限、未授权根、缓存格式、TOML 和评估命中身份。远端聊天仅模拟 HTTP，不代表真实模型引用质量或视觉效果；没有调用付费 API。提交前还核对注释迁移与架构，主线门禁执行全量 pytest。

21项新增场景通过；两组相关回归分别143项（26.59秒）和227项（99.35秒）通过，Ruff、521个文件格式与 Pyright 通过。首次新测试错误地用文本匹配嵌套 JSON、随后误把持久 Message 当字典，均已改为解析实际 Message.content 并核对来源；修正后包含完整重放验收。注释差异已逐项核对，旧解析错误说明迁移到共用 _open_pdf，其他关键契约保留并补充 PDF 位置语义。


## B17.3.1 图片分析扩展 API

[ImageAnalyzer](../adam/contracts/perception.py) 定义 ImageRequest 和 ImageReply；[inspect_image](../adam/documents/image_analysis.py) 接收调用方已经授权读取的原字节、MIME 声明和问题，计算真实字节版本与问题摘要，至多调用一次显式注入的分析器。默认没有分析器，返回 status=unavailable、reason=image_backend_not_configured，不打开网络或进程，也不从文件名猜测画面。

本步是供后续集成的 API，**没有注册聊天图片工具，没有内置视觉模型后端，也没有完成 OCR、图片解码或 PDF 页面渲染**。MIME 声明只允许 PNG/JPEG/WebP，不证明字节是合法图片；具体后端须验证解码格式和资源。不能把这个接口的完成描述为 Adam 已能看图。未来连接收费模型时，还必须使用已有请求日志、共享额度、取消和未知用量停止机制，不能直接在普通文件工具中偷发一条未记账的 HTTP 请求。

原字节默认最多1 MiB、可显式设到8 MiB；问题最多2000字符，分析文本默认最多4000、上限8000字符。模型身份和输入/输出 token 必须格式有效；未知用量保持 None，不补零。错误、超长或无法保存的文本明确失败，不交付部分描述或自动重试。原预算、存储及取消异常保持原语义，未分类后端异常不回显远端正文。

返回 ImageInspection 不包含图片字节，保留 source_version、size_bytes、mime_type、question_sha256 和分析来源。independently_verified 始终为 false：模型分析不是可直接核验的原文，也不自动标成正确事实。请求对象隐藏字节的 repr 仅避免调试输出误打印，不是脱敏存储机制。

[24项契约场景](../tests/features/test_image_analysis_contract.py) 覆盖无后端时零网络/进程、原字节及哈希传递、单次调用、未知用量、输入/输出边界、模型身份、编码失败、预算/保存故障和调用前后取消。连同 PDF 三组场景及架构共97项通过（7.15秒）。测试使用不透明合成字节和预设分析器，明确不验证实际像素理解；未调用付费 API。B17.3.2 继续进行文本 PDF 的组合验收并记录真实模型/视觉待验收范围。
