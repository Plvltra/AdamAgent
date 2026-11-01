# 录制分享副本与本地脱敏

B13.1 提供显式宿主 API [export_share](../adam/recording/sharing.py)。它将一份已验证录制转换为独立 JSON 分享副本，包含脱敏事件、替换位置报告和来源摘要；不改写原录制、不调用模型或工具，不发送任何数据到外部。B13.2 已接入下方 CLI 和来源核对入口。

## 怎样使用 API

```python
from pathlib import Path

from adam.recording.redaction import LiteralRedaction
from adam.recording.sharing import export_share

report = export_share(
    Path("turn.recording.jsonl"),
    Path("turn.share.json"),
    literals=(LiteralRedaction("虚构市练习路 42 号", "address"),),
)
```

输出路径必须不存在；不会覆盖已有副本、原录制或指向它的硬链接。返回 report 只有来源字节摘要、事件/替换数量及类别统计，不带原敏感值。打开分享文件核对正文及 findings 后再自行分享，程序不执行上传。

## 规则与报告

[redaction](../adam/recording/redaction.py) 首先识别候选值，然后替换整份 JSON 中重复出现的相同文本，包括未加标签的重复值。支持结构化 JSON 的字符串键、值和列表；原 JSON 容器顺序及普通数值保持，原输入对象不修改。

| 规则 | 范围 |
| --- | --- |
| email | 有限形态的邮箱文本；不是完整国际邮箱规范解析器 |
| credential | sk- 形式、Bearer 形式及 api_key/password/secret 等常见赋值文本 |
| 敏感字段 | password、passwd、secret、api_key、access_token、refresh_token、Authorization 的完整值；忽略键的大小写、下划线及连字符 |
| 地址字段 | address、home_address、postal_address、地址、住址的完整值 |
| LiteralRedaction | 用户明确给出的精确文字，可标为 email、credential、address、identifier 或 text；适用于任意地址、姓名和其他文本 |

同一导出内识别到的相同文本使用同一占位符；每次导出随机命名，原文到占位符的映射只在内存存在，不写到报告。位置使用原容器的序号，例如 `$[2].value[0]`；字典键可能含隐私，因此报告不直接复制键名。结构化敏感字段可整体被替换成字符串，分享结构不保证维持原业务类型。

规则版本 local-redaction-v1。显式字面规则最多 128 条，每条 1～4096 字符；自动与显式的不同候选值合计最多 256 个，替换最多 10000 处，遍历最多 500000 节点、深度最多 64。超限停止，不发布部分脱敏结果。候选按最长文本优先匹配，不允许用户提供任意正则。

## 来源与保存边界

原文件只读取一次。[parse_recording](../adam/recording/journal.py) 使用同一字节缓冲验证原协议，SHA256 也来自这份缓冲，避免两次读取活动文件时错绑内容。保留来源字节数、完整/残缺尾行状态和有效事件数；只带出完整、已验证事件，不带出被截断尾行的原始内容。没有结尾仍标为不完整，导出不会修复源录制。

默认遵守原录制的 32 MiB 文件、4 MiB 单事件和 10000 事件限制，输出最多 64 MiB。先把已脱敏结果写入输出同目录临时文件，flush/fsync 后通过排他硬链接创建目标；目录不支持该操作就明确失败，不退回覆盖写入。可捕获失败会清理临时文件，不发布半份副本。

分享容器格式为 adam_shared_recording_v1，purpose 为 human_review_only，manual_review_required 始终为 true，replayable 始终为 false。来源摘要用于本地核对，不是数字签名，也不是“脱敏完整”的证明。原始录制保持原字节，仍是重放依据；分享副本不能交给原重放路径当作无损录制。

## 能力边界与验证

规则会漏掉无标签地址、任意姓名、编码/拆分/改写的秘密或不符合已知形态的凭据；也可能误替换公开示例或名为 address 的技术字段。嵌在字符串里的 JSON 当前仍按普通文本扫描，不能据此声称已经理解所有嵌套语义。用显式字面规则补充，并审阅副本。即使替换数量为零，也只能说明没有规则命中，不能说明内容没有隐私。

[16 项业务场景](../tests/features/test_recording_sharing.py) 使用虚构资料及真实文件工具/录制，覆盖邮箱、凭据与指定地址、重复值、报告字段、原字节保持、同名/硬链接拒绝、fsync/发布故障、残缺录制及资源限制。与原录制协议和架构检查共 70 项通过，Ruff、Pyright 通过；提交门禁运行全量 pytest。未调用付费 API，未分享或上传真实用户资料。


## B13.2 显式命令与私有规则

```powershell
python -m adam.recordings share turn.recording.jsonl --output turn.share.json
python -m adam.recordings share turn.recording.jsonl --output turn-with-rules.share.json --rules private-rules.toml
python -m adam.recordings inspect-share turn.share.json
python -m adam.recordings inspect-share turn.share.json --source turn.recording.jsonl
```

这两种命令不加载 llm.toml、不发模型请求，也不运行录制里的命令或工具。share 只打印无原文摘要；成功退出码 0。inspect-share 不带 --source 时 source_binding 为 not_checked；显式来源字节和 SHA256 一致时 matches、退出 0，不一致时 different、退出 1；格式或 IO 错误退出 2。

私有规则 TOML 的例子如下，文件最多 64 KiB。规则属于本机私有材料，导出只保存数量；不要把它与分享文件一起上传。省略 category 时默认为 text，version 必须为整数 1，不支持任意正则或未知字段。

```toml
version = 1

[[literals]]
value = "虚构市练习路 42 号"
category = "address"

[[literals]]
value = "练习用未加标签的秘密"
category = "credential"
```

[share_format](../adam/recording/share_format.py) 独立校验格式、待审/不可重放标记、事件和替换数量、类别统计、时间以及 JSON 复杂度。拒绝重复字段、非有限数、额外字段、未知版本和超限文件；错误不回显原文。查看不会自动读取正文中的路径，也不恢复占位符的原值。可选 --source 核对默认最多 32 MiB。

来源字节核对不是数字签名：有人改写分享正文而保留原来源摘要时，source_binding 仍可能 matches。因此报告始终保留 privacy_quality=not_verified 和 manual_review_required=true；格式校验不能判断是否仍含隐私、引文是否被恶意改写或答案是否正确。

分享副本仍与录制协议不同，replay 读取后明确失败，不创建重放输出目录。不能通过修改 replayable 标志获得重放资格，也不提供去掉标记后无损恢复的入口；需要重放时使用本地原始录制。

[22 项场景](../tests/features/test_sharing_commands.py) 覆盖实际 CLI、私有规则和错误不回显、来源匹配/变化、分享拒绝重放、坏格式/复杂度及“来源摘要匹配不代表正文未改写”。与导出 API、原重放流程和架构共 70 项通过；Ruff、Pyright 与提交全量门禁分别核对。B13.3 继续覆盖嵌套参数、工具输出、漏报和误替换样本。
