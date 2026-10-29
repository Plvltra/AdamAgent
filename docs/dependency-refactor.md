# 应用依赖边界重构

日期：2026-10-29。重放到 main 的 8fc92bf 之后，保留 V1.3、V3 和 V4.1b 已提交功能。

## 查到的问题及处理

| 原问题 | 修改后 |
| --- | --- |
| CLI 创建 HTTP、模型、存储、注册表和 Conversation | bootstrap 集中创建，CLI 取得一个已组装应用 |
| CLI 绕过流程访问 conversation.store 和 limits | 应用提供列表、历史、用量查询；返回独立数据 |
| CLI 调用模型目录并决定是否允许切换模型 | 应用负责目录获取及切换规则，CLI 只解释命令和展示结果 |
| CLI 导入多个底层异常和命令实现，只为显示错误和确认 | 共享 CommandRequest；公共 AgentError 及 partial_reply 交给统一展示函数 |
| Conversation 从 client.config 读取模型并隐式创建工具 | 构造时注入默认模型和完整执行器；模型只需实现 ModelClient |
| 配置解析通过工具包取得配置类型，间接加载执行实现 | settings 定义纯配置值，config 只负责加载校验 |
| Session、Completion、CommandRequest 等数据依附具体实现文件 | models 提供共享数据，使用者无需因类型而导入实现 |
| ToolRegistry 构造具体工具，并按 run_command 名称决定执行路径 | factory 组装适配器，registry 查表调用；命令输出编码器单独注册 |
| CommandTools 接收只用到 workspace 的 PathPolicy | 只传工作目录；通用命令和只读工具的权限边界明确区分 |
| 执行协调层生成终端专用“工具>”文字 | 只通知工具名称，CLI 决定展示样式 |
| HTTP 和会话关闭职责分散在 main 与 chat | open_application 关闭自己创建的资源；外部依赖由调用者关闭 |

## 提交检查修正

测试初始化临时 Git 仓库时，必须清除从提交钩子继承的 GIT_* 环境变量；仅传 -C 不能覆盖 GIT_DIR 等上下文。tests/test_tools.py 的 fixture 在测试进程内隔离这些变量，避免测试提交、暂存和配置写入真实仓库。

## 保留的合理依赖

Conversation 仍需要存储和工具执行能力，LLMClient 仍需要 HTTP 与 LLMConfig，文件工具仍需要 PathPolicy。这些是模块职责需要的协作，并非只要看到 import 就继续拆分。

完整配置不会逐层广播。bootstrap 读取 AppConfig，再把模型连接配置、工具限制、权限和运行额度分别交给相关组件。没有引入全局配置、服务定位器或通用插件框架。

## 行为与验证

- 主线原有测试场景及参数化分支保留，更新测试依赖组装位置；不削弱业务断言。
- 应用可使用没有 config 或 HTTP 属性的模型完成 CLI 聊天、切换模型和查询用量。
- 新工具通过注册即可进入原有循环、参数校验和执行留痕。
- 启动失败、恢复失败、正常退出或界面异常后，HTTP 关闭且持有的会话锁释放。
- 沿用主线 AgentError 契约，原样传递未保存回答和 cause；存储边界转换 IO 故障，终端意外故障继续传播。
- 分层测试防止 CLI、配置和运行流程重新引用不该依赖的实现。

旧基线曾通过 164 个用例；本次集成以 tests/features/README.md 的最新回归结果为准。测试收集无遗漏或重复，详细范围见 tests/features/README.md；使用假模型，不消耗 API 额度。

API 请求、记账与工具副作用的先后顺序保持；失败轮次不进入有效历史，未知用量及未知命令结果仍阻止后续调用。原始日志格式和磁盘数据无需迁移。

注释审查：配置、数据类和异常类说明随定义迁移；命令结果裁剪说明随编码器移动。CLI 的资源关闭说明更新为组装入口所有权，原有请求记账、日志失败与权限说明保留。

架构图及每个模块的职责见 [architecture.md](architecture.md)。最初重构接在 main 的 V4.1b 之后；V5 完成后按用户确认合并主线 V4.1c～V4.2b，再将 main 快进至集成结果。编辑继续在 factory 注册，执行事实由 AgentApplication 提供；没有恢复 CLI 对 Conversation/存储实现的直接访问。最初清除过 Git 测试误写的仓库设置，临时仓库测试继续隔离 GIT_* 环境。
