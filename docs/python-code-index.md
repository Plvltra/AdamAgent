# T10 Python 语法感知代码定位

更新日期：2026-10-31

## 需求与阶段

目标是让 Adam 按代码结构查找定义和有依据的引用，补充已有文本搜索。完整需求仍以 [能力扩展任务](agent-maturity-tasks.md) 为准。

| 阶段 | 交付内容 | 状态 |
| --- | --- | --- |
| T10.1 | AST 结构、带作用域的符号、导入别名、有界扫描、内容版本与增量刷新 | 已实现，23项业务场景及21项架构约束通过 |
| T10.2 | 结构、定义、静态引用查询工具，来源、行号、版本和输出限长 | 已实现，32项新增业务场景、102项相关回归通过 |
| T10.3 | 真实小项目定位、动态情况、权限和不执行目标源码的整体工具验收 | 待实现 |

T10.1 提供独立索引 API；T10.2 已向主/子模型注册三个原生只读工具，沿原调用、权限、模式、日志和预算链路执行。T10.3 的完整小项目任务及组合故障验收保持待办。

## 已实现的结构与身份

- CodeModule：同一次文件读取的绝对路径、相对路径、模块标签、原字节 SHA-256 版本，以及结构或诊断。
- Definition：模块、类、普通/异步函数；包含限定名、父作用域 ID、定义行/结束行及文档字符串首行摘要。
- ImportBinding：导入所在作用域、局部别名、目标模块、导入成员及相对层数；不执行导入；查询仅连接索引内唯一的显式导入链，不推断动态导出。
- CodeSnapshot：一次完整扫描的不可变结果、generation 与跳过项数量；find_definitions 按短名、限定名或 ID 返回全部匹配。

模块标签来自相对索引根的路径：pkg/mod.py 是 pkg.mod，pkg/__init__.py 是 pkg；根目录自己的 __init__.py 使用 `<root>`。这是当前索引范围的结构标签，不模拟 sys.path、环境安装或运行时包加载。

符号 ID 包含源路径、限定名、定义行、AST 列偏移及种类。作用域指向实际父定义：两个类的同名方法、嵌套函数、同一作用域不同位置的重复定义均分别保留，不以函数名去重。行号从1开始；列偏移只参与身份，采用 AST 的 UTF-8 字节位置。文件版本独立保存，位置 ID 不冒充内容版本。

## 刷新、错误和权限

每次 refresh 都重新授权并有界读取源字节，不只比较修改时间。相同内容复用解析结果；新增/更新重新解析，删除从新快照消失。缓存只在内存中，整个扫描成功后才替换，不在项目写数据库或缓存文件。

单文件语法、编码、读取或语法树超限产生明确诊断，该文件不保留旧定义。完整字节已读取时可记录版本；仅读到上限或 IO 失败时版本未知。整体文件数、目录项、总字节、总符号或超时超限会使查询失败，取消原样传播，不返回旧快照。

宿主必须提供 authorize 回调；当前示例和测试注入原 PathPolicy.resolve。根、目录及读取前再次检查；权限撤销不能继续使用缓存。递归跳过 .git、虚拟环境、缓存目录、node_modules，以及符号链接和 Windows junction；即使链接目标另行授权，扫描也不递归穿过链接，可由用户明确指定那个已授权目录。

这里只读取 .py 文件。按标准库 detect_encoding 检查编码声明和 BOM，再接受 UTF-8/UTF-8 BOM；声明了其他编码或二者冲突会得到诊断，避免把源码解释成另一套名称。[Python 编码检测说明](https://docs.python.org/3.11/library/tokenize.html#tokenize.detect_encoding)

解析调用 ast.parse，目标语法为 Python 3.11，不执行源码、导入目标模块或计算表达式。得到 AST 不代表代码通过完整编译、类型或运行验证。[Python AST 文档](https://docs.python.org/3.11/library/ast.html)

## 默认资源上限

| 限制 | 默认值 |
| --- | --- |
| 扫描目录项 | 5000 |
| Python 文件 | 512 |
| 单文件原字节 | 1 MiB |
| 累计读取原字节 | 16 MiB |
| 单文件 AST 节点 / 深度 | 100000 / 100 |
| 定义与导入总数 | 20000 |
| 名字使用总数 / 单文件上限 | 100000 / 100000 |
| 协作扫描时间 | 5秒 |

CodeLimits 可由宿主显式构造，参数有上限及类型校验。超时检查在扫描、分块读取及 AST 遍历边界执行，包含解析前后；它不是对一次底层文件 IO 或 AST C 层解析的硬抢占。解析前另有字节限制。没有新增 TOML 段。工具组装读取现有 tool_limits 的 max_scan_entries、max_text_file_bytes、scan_timeout_seconds 和 max_result_chars，并受 CodeLimits 硬上限约束；查询从扫描到引用解析共用截止时间。只读工具每次独立建索引，避免并发共享可变状态；显式 CodeIndex API 则可复用自身的内存缓存。

## 模型工具与结果

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| code_structure | path、可选 offset/limit | 模块、类、函数、导入及文件诊断 |
| find_definitions | path、name、可选 offset/limit | 短名、限定名或完整符号 ID；保留所有同名候选 |
| find_references | path、name、可选 offset/limit | 目标唯一才解析静态引用；歧义时先返回定义候选 |

path 是索引范围，相对工作目录解析；跨文件引用应指定含目标和使用处的目录。目录标签必须与源码导入前缀对应：Adam 的 `adam.*` 导入应从项目根 `.` 查询。指定单个文件只分析该文件；不会为补齐导入而擅自读取范围外文件、已安装包或 sys.path。

offset 从0开始，limit 为1～100，默认20。JSON 只截取完整条目，返回 next_offset、truncated、total 与 generation；翻页时 generation 变化应从头查询。单条装不下时返回 output_limit，不跳过它或伪造完整来源。文件诊断排在前面，status=incomplete；记录内保留路径、原字节版本及可知行号。忽略目录/未授权项计入 skipped_entries，不泄露被保护内容。

静态引用含 definition_id、源路径/版本、起止行、从0开始的 UTF-8 字节列和 reason。unresolved_total 是整个扫描范围的未知名字使用数，包含外部库及内置名字，**不是目标的引用数**；results 仅另外列出与目标末段同名的未知线索。没有匹配不能证明没有运行时引用。导入声明单独列在结构结果，不当作名字使用重复计数。

## 静态引用边界

- 支持唯一的模块/函数词法定义、嵌套函数闭包、索引内显式 import/from 别名、相对导入、唯一重导出链；导入循环与超过32层保持未知。
- 方法查外层名字时跳过类体作用域；默认值、装饰器、参数注解、返回注解和类基类按其语法外层收集。这里只表示源码绑定，包含注解中的使用，不承诺执行发生或调用成功。
- 参数、赋值/删除、except/match 绑定、条件定义/导入、多重定义、已发现的模块属性重绑定均不猜运行结果。global/nonlocal 和星号导入采用整份文件保守降级。
- 类体名字、lambda、推导式、实例/调用结果属性、动态 getattr、外部模块和运行时 monkey patch 不做完整推断。lambda/推导式中的名字明确未知，可能影响外层的海象绑定也阻止误认旧定义。
- 模块标签重复、普通模块与包路径冲突、目标文件失败，均保留未知；不会导入代码来“确认”结果。

这是有界的静态定位工具，不替代 Python 编译器、Pyright 或完整调用图。词法规则的依据见 [Python 名字绑定与解析](https://docs.python.org/3.11/reference/executionmodel.html#resolution-of-names)。结果中的注释、名称、摘要均视为参考数据，不能改变宿主权限或审批策略。

三个工具注册 READ_ONLY 能力、路径选择器及独立取消接口，在 plan 模式和子任务中可用；由现有调度器控制并发，复用实际工具开始/结束事件及工具调用额度。未改变 Conversation、CLI 或存储协议。

## 显式使用

以下示例只扫描当前目录及宿主授权范围：

~~~python
from pathlib import Path

from adam.code_index.index import CodeIndex
from adam.config.settings import PermissionConfig
from adam.tool_calls.policy import PathPolicy

workspace = Path.cwd()
policy = PathPolicy(workspace, PermissionConfig())
snapshot = CodeIndex(workspace, policy.resolve).refresh()
for symbol in snapshot.find_definitions("Conversation"):
    print(symbol.path, symbol.start_line, symbol.qualified_name)
~~~

错误文件可通过各 CodeModule.issue 查看。调用方应同时展示诊断，不能把索引失败描述成“文件里没有这个定义”。

## 验收证据

T10.2 的 [32项查询业务场景](../tests/features/test_python_code_tools.py) 覆盖导入别名/相对导入/重导出、方法与闭包、各类遮蔽及未知、包冲突/重绑定、引用数量上限、歧义选择、更新删除、完整分页、坏参数，以及假 HTTP + 真实文件工具 + JSONL + 用量统计。加上原索引、架构、工具循环和并发注册检查，共102项通过。完整 Ruff、Pyright 和全量 pytest 由本项独立提交钩子执行。

实际用工具从 Adam 项目根查询 Conversation，得到定义 adam/application/conversation.py:48，以及15条静态引用/同名未知线索，无文件诊断。该数量只是当前工作树的观察，不是固定基准；从 adam 子目录查询时部分绝对导入不可连接，工具如实返回未知。未调用付费模型。

[23项业务场景](../tests/features/test_python_code_index.py) 使用真实临时 Python 文件与生产 PathPolicy，覆盖：

- 嵌套/重复/跨模块同名、异步函数、类方法、导入别名和相对导入、真实行号及版本；
- 同时间戳内容更新、删除、未变内容复用、坏源码清除旧定义及恢复；
- 顶层写文件语句不执行、保留目录跳过、权限撤销、额外根授权和真实目录链接；
- 文件读取故障、各层数量/字节/AST 限额、超时、取消和编码声明冲突。

另有21项架构约束通过，Ruff 和 Pyright 通过。本次以默认限额扫描当前 Adam，得到277个 Python 文件、2621个定义、3158条导入且无诊断，Conversation 定位到 adam/application/conversation.py 第48行；此为本次工作树的实际检查，不是固定性能承诺。

当前模块依赖见 [系统架构](architecture.md)。T10.3 的真实小项目完整任务及组合故障验收仍待完成；真实模型选择工具的效果也不由模拟 HTTP 证明。
