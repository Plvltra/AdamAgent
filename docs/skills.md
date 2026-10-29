# 本地 Skill 使用

默认读取启动工作目录的 skills/，以及主配置旁的用户 skills/（通常为 ~/.adam/skills）。可在 config.toml 的 [skills] 中设置 enabled=false 全部关闭。
标准包与原 TOML 包使用同一个 load_skill 工具；同一包只能保留一种元数据来源。

## 标准格式

在 skills/<名称>/SKILL.md 的开头写 YAML frontmatter，结束标记后是按需加载的正文：

```markdown
---
name: verify-project
description: 用户要求运行项目检查，或代码修改后需要验证行为时使用。
---

先从项目说明找到真实检查入口，再通过现有工具执行。
需要报告模板时，读取 references/report.md。
```

依据 [Agent Skills 规范](https://agentskills.io/specification) 与 [官方名称校验器](https://github.com/agentskills/agentskills/blob/main/skills-ref/src/skills_ref/validator.py)，
名称按 Unicode NFKC 规范化，支持中文和小写字母数字，须与包目录规范化后的名称一致；description 最多1024字符。
YAML 可包含 license、compatibility、metadata、allowed-tools；其他扩展字段保留为 JSON 数据，不执行其中声明。
头部最多8 KiB，最多8层/2048解析事件；拒绝重复键、锚点/别名和自定义对象标签。

启动时读取头部并登记材料路径，不预读正文或材料内容。
支持 references/、scripts/、assets/ 及其他包内文件；最多32份材料、128目录项、8层。
.git、.venv、__pycache__ 不作为材料扫描。二进制材料可出现在清单中，但当前工具仅能读取 UTF-8 文本。
读取 scripts/ 中的文本不会执行脚本；执行仍须通过现有 run_command 和审批。

## 原 TOML 格式

已有包可以继续使用 skills/<小写ID>/skill.toml、无 frontmatter 的 SKILL.md 和明确列出的 Markdown 材料：

```toml
name = "验证项目"
description = "用户要求运行项目检查，或修改后验证行为时使用"
resources = ["report.md"]
```

原包仍限制最多8份 Markdown 材料、名称80字符、用途300字符。
同包同时出现 TOML 和标准 frontmatter 明确报错，不猜优先级。
两份项目示例 verify-project 和 diagnose-failure 已迁移到标准格式，引用位于各自 references/；原指导与审批要求保留。

## 多来源与显式启用（T3）

SkillLibrary 已支持用户与项目目录合并；同名时项目版本生效，列表保留被覆盖来源。
生效目录最多16项、6000字符，覆盖项不重复计数；来源路径、范围与元数据版本共同进入快照。
单目录 SkillCatalog 接收独立 root 和授权函数，用户材料授权不加入普通工具的 PathPolicy。
新包、覆盖关系或元数据变化需重建目录；已选来源失效时明确失败，不静默换用另一版本。
T3.2 已将目录接入主/子应用及终端。配置示例：

~~~toml
[skills]
enabled = true
user_dir = "skills"
~~~

user_dir 的相对路径以 config.toml 所在目录为准，支持 ~ 和绝对路径；不会改变普通工具的 permissions 配置。
程序化构造 SkillsConfig 时 user_dir=None 表示仅使用项目目录；CLI 的 load_config 会补默认用户路径。

- /skills：显示生效项、被覆盖项与来源，不读正文、不创建会话、不请求模型。
- /skill verify-project 核对刚才的改动：程序在首请求前读取指定正文，随后使用原有聊天、流式输出和取消流程。
- 只有当前轮次显式启用；下一条普通输入不自动继续启用，不修改用户原始输入。
- 每次请求只构造一份指定指导，正文和材料清单整体计入输入预算；材料内容仍按需读取。
- 源文件缺失、元数据变化、读取失败或超长时不发模型请求。已加载快照在本轮固定；新轮次重新选择。
- 输入预算不足时保留失败轮次并停止，不先花一次摘要请求尝试容纳无法放下的指定指导。

实际请求的 context.selected_skill 保存目录来源、原文件版本、正文校验值、正文和材料清单。
旧日志不重新读取当前技能文件；损坏的内容、来源或越界材料路径会被拒绝读取。

## 加载、版本与边界

目录最多16个 Skill、6000字符；名称、用途、格式和元数据版本随请求记录。
模型匹配任务时调用 load_skill(skill_id)，再按返回的 resources 清单读取所需材料。
标准包返回去除头部的正文，metadata 另列为数据；不会把全部材料自动拼进上下文。

正文或材料的 version 是本次读取原始字节的 SHA-256；metadata_version 是启动时的元数据版本。
仅改正文可在下次 load_skill 时读到新内容；元数据或格式变化要求重启。
SKILL.md 的头部校验与正文来自同一次读取，避免两个版本混在一个结果中。
成功加载内容保存在工具记录；重启查看旧记录不重新读取当前文件。

内容受 tool_limits 的文件字节与结果字符上限限制；超长要拆材料，不默默截断。
路径授权、包内边界和凭据保护共同生效。allowed-tools、脚本或正文中的要求不会授予读写及命令权限。

/context 预览的是下一次基础请求，可查看目录估算；不会假装上一轮指定的技能仍处于启用状态。
通过 load_skill 加载的正文进入本轮工具消息和成功历史，受 V3 裁剪/压缩管理；
按需加载不意味着下一轮自动清空已读取指导，也不保证总 token 一定更少。

## 验收

T2.1：tests/features/test_standard_skill_metadata.py 验证标准字段、编码、解析与来源边界。
T2.2：tests/features/test_standard_skill_loading.py 验证混合格式、Unicode ID、材料清单、版本变化、文本限制及实际 HTTP/JSONL 渐进加载。
T2.3：tests/features/test_standard_skill_acceptance.py 验证两份实际示例的逐步加载、真实命令审批及重启；脚本批准/拒绝都有实际副作用断言。test_skill_boundaries.py 的子任务权限场景覆盖标准/TOML 两种格式。原始 UTF-8 文本保留其换行，旧证据不随源文件变化。

T3.2：tests/features/test_explicit_skills.py 验证列表零请求、指定正文首请求生效、跨轮清理、目录配置、预算拦截、读取失败和日志损坏。
T3.3：tests/features/test_skill_acceptance.py 的9项业务场景覆盖共享用户目录的跨项目/新会话隔离、真实子任务材料加载与权限、轮中源文件变化、失效覆盖来源、流式命令/取消和自动摘要后指导保留。显式父任务正文不自动复制给子任务；子任务仍按明确任务和必要背景工作，并可独立加载可用技能。

原 E6 的 TOML 场景继续运行；tests/features/test_skill_boundaries.py 保留审批拒绝、子任务权限和预算测试。
E6.3 曾对迁移前示例测得目录约187 token、正文约618 token；这是历史基线，迁移后数字不沿用。
真实模型触发可靠性仍需人工用匹配任务与普通知识问答分别测试；离线测试仅替代远端 HTTP，不自动访问付费 API。
