# 本地 Skill 使用

默认读取启动工作目录的 skills/。可在 config.toml 的 [skills] 中设置 enabled=false 关闭。
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

/context 可查看目录估算。正文进入本轮工具消息和成功历史，受 V3 裁剪/压缩管理；
按需加载不意味着下一轮自动清空已读取指导，也不保证总 token 一定更少。

## 验收

T2.1：tests/features/test_standard_skill_metadata.py 验证标准字段、编码、解析与来源边界。
T2.2：tests/features/test_standard_skill_loading.py 验证混合格式、Unicode ID、材料清单、版本变化、文本限制及实际 HTTP/JSONL 渐进加载。
T2.3：两个项目示例的完整审批与恢复验收仍列在能力扩展需求中；不因加载器已完成而提前勾选。

原 E6 的 TOML 场景继续运行；tests/features/test_skill_boundaries.py 保留审批拒绝、子任务权限和预算测试。
E6.3 曾对迁移前示例测得目录约187 token、正文约618 token；这是历史基线，迁移后数字不沿用。
真实模型触发可靠性仍需人工用匹配任务与普通知识问答分别测试；离线测试仅替代远端 HTTP，不自动访问付费 API。
