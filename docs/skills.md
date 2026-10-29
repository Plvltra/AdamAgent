# E6 本地 Skill 使用

默认读取启动工作目录的 skills/。可在 config.toml 的 [skills] 中设置 enabled=false 关闭。
每个 Skill 是 skills/<小写ID>/skill.toml、SKILL.md 和可选材料：

```toml
name = "验证项目"
description = "用户要求运行项目检查，或修改后验证行为时使用"
resources = ["report.md"]
```

这是 Adam 的本地 TOML 包格式，不是通用 Agent Skills frontmatter 格式。
目录最多16个 Skill、6000字符；单包最多8份 Markdown 材料。
名称和用途随每次请求发送，目录变化后重启应用；正文随 load_skill 按需读取。
示例 verify-project 和 diagnose-failure 都先查项目实际入口，不绑定某个测试框架。

模型在正常请求中选择 load_skill(skill_id)，需要分支资料时再指定 resource。
名称、用途、元数据版本写入请求记录；正文、材料名与字节版本写入普通工具记录。
正文不能超出 tool_limits 的文件字节与结果字符上限，超长要拆材料，不默默截断。
路径授权、包内边界和凭据保护共同生效；Skill 不授予读写或命令权限。

/context 可看到 Skill 目录的估算。正文进入本轮工具消息和成功历史，受 V3 裁剪/压缩管理；
按需加载不意味着下一轮自动清空已读取指导，也不保证总 token 一定更少。

离线测试会指定模拟模型加载匹配说明、忽略无关说明，并核对实际 HTTP 内容。
真实模型触发可靠性需人工用“验证项目”“定位测试失败”“普通知识问答”分别测试，记录误触发与漏触发；
本轮不会自动访问付费 API。

E6.3 离线对照：当前两份示例的目录数据约187 token，两份正文约618 token（应用估算，未计提示包装和工具 Schema）。
这里只比较尚未加载正文时的内容体积；加载工具、后续历史以及服务端实际计费另计。
审批拒绝、子任务权限保持和超预算零请求均有可运行场景：tests/features/test_skill_boundaries.py。
