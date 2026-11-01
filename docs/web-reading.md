# 网页读取（C00.1 / C01.1）

当前范围：独立网页读取服务、命令行入口与可选主/子工具接入；不包含搜索、浏览器、模型自动选择来源或引用质量验收。

## 使用

在项目目录使用现有 Python 环境运行：

```powershell
python -m adam.web https://example.com/
```

输出 JSON 包含 `schema_version=1`、`requested_url`、`final_url`、`fetched_at`（UTC）、`sha256`（原始实体摘要）、`byte_size`、`title`、`text`、`content_type`、`truncated`、`redirects`。命令不创建模型客户端、会话或调用付费 API。成功返回 0；受控失败返回 1，输出安全的 `error_type`；中断返回 130。

程序接口：`WebReader(transport, policy, limits).fetch(url, cancel=token)`。调用方拥有 `HttpPageTransport` 并须在 finally 关闭；服务只借用窄 `PageTransport`。不传全局配置或 SessionStore。

默认上限：实体 1 MiB、正文 16000 字符、4 次重定向、完整抓取期限 15 秒。字符截断有显式标记；字节超限不返回快照。默认 UTF-8，采用响应声明的 charset，非法字节替换为 U+FFFD，不承诺自动检测历史网页编码。

## 网络与资料边界

- 仅允许 HTTP(S) 公共地址和 80/443 端口；拒绝 URL 凭据、控制字符、内网、环回、链路本地、映射 IPv4、保留/组播地址。DNS 全部结果均须合规，不能只验证第一个。
- 每次连接与重定向重新检查，固定已验证 IP 连接，保留原始 Host/TLS SNI 与证书验证；禁用 keepalive，避免多个域名共用 IP 连接而绕过 TLS 主机验证。无自动重试、代理、认证或跨请求 cookies。
- 测试仅由可信宿主注入精确的环回 origin，普通 CLI 没有内网放行选项。这不是防火墙替代品，部署仍应限制出口。
- 仅接受 HTML/XHTML/纯文本。请求 identity 并拒绝压缩响应，避免解压炸弹；不执行 JavaScript，不读取子资源。HTMLParser 去掉脚本/样式等，不能替代完整浏览器可见性计算，动态网站交给 C02。
- 网页内容始终是不可信资料，不授予执行权限。此阶段只返回不可变来源快照；来源持久化、片段引用与聊天闭环由 C01.3 完成。
- 超时涵盖抓取等待；取消会终止异步连接和读取。系统 DNS 使用平台 resolver，原生 DNS 调用若阻塞，事件循环关闭可能等待其返回，不能承诺操作系统 DNS 的硬取消时限。无自动重发。

## 可选工具接入（C00.1）

主配置按需添加（示例配置已包含默认关闭项）：

```toml
[web]
enabled = true
max_bytes = 1048576
max_chars = 4000
max_redirects = 4
timeout_seconds = 15
```

启用表示允许原工具管线读取策略内的公共网页，不放宽文件/命令权限。默认关闭时不注册 `read_webpage`、不导入网页后端；开启但普通聊天不读取时也不创建连接。当前后端复用项目已有 HTTPX，无新增必装 SDK。配置没有私网放行或代理绕过键。

默认按需发现：先 `discover_tools(names=["read_webpage"])`，在后续模型请求调用 `read_webpage(url=...)`。工具返回 `status=fetched` 及完整 `source` 结构；正文可进一步限长，但来源 URL/摘要不会被通用预览悄悄替换。连元数据都放不下时返回 `output_limit`，不能视为有效引用；抓取失败以 `error_type` 分类。搜索及稳定来源 ID/片段映射仍属于 C01.2/3。

主任务与子任务使用同一注册函数。只读声明、发现名单、模式、工具总次数、Hook、日志、取消及录制重放均复用现有管线。每个实际调用在所属工作线程通过 `open_reader` 创建/关闭独立资源，支持两个安全读取并发，不共享 HTTP 客户端、cookies 或事件循环；启动/闲聊没有需长期持有的网页资源。构造失败也释放已创建部分，清理失败给原异常加安全说明而不覆盖它。若系统 DNS 阻塞导致读取线程不能及时退出，沿用既有 ToolCleanupError 停止后续派发，不宣称后端已硬取消。

验收入口：`python -m pytest -q tests/features/test_optional_web.py tests/features/test_web_reading.py tests/test_web_config.py tests/test_web_policy.py tests/test_architecture.py`，97 项通过（新增 13 项业务、11 项配置）。模型 HTTP 使用 MockTransport，但网页服务、主子线程、Hook 检查器、JSONL、关闭与取消均实际执行；离线重放零重新连接。原 C01.1 的 21 个场景迁移共享夹具后没有遗漏或重复。

本机 pytest 9.1.1 的 conftest 按目录节点匹配；同次命令交错重复进入父/子目录时可能出现同名目录节点而漏匹配夹具。定向命令将同目录文件放在一起，全量用单一 `tests`/默认 testpaths 入口；未修改 pytest、未跳过测试、未改变权限。已核对全仓 1890 项收集结果及单入口夹具可见性。

2026-11-01 12:13（Asia/Shanghai）新 CLI 已真实读取 `https://example.com/`，标题 Example Domain、实体 577 字节、SHA-256 `25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484`；TLS SNI/证书验证保持开启。该一次后端验证不证明搜索或模型效果，不作为日常联网自动测试。

## 验收

`python -m pytest -q tests/features/test_web_reading.py tests/test_web_policy.py tests/test_architecture.py`：73 项通过。故障注入只在 DNS 边界模拟解析失败/重绑定，其余连接、重定向、正文、超时和取消均为真实本地 ThreadingHTTPServer，不使用 MockTransport。没有迁移或删除已有场景。

Ruff format/lint、Pyright 与全量 pytest 是每项提交门禁。真实互联网搜索、模型选择来源与五题对照尚未运行，不用这些本地测试代替 C01.2～4。
