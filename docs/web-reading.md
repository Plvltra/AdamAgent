# 网页读取（C01.1）

当前范围：独立网页读取服务及命令行入口；不包含搜索、浏览器、模型自动选择来源或引用质量验收。C00.1 随后接入已有工具注册与资源生命周期。

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

## 验收

`python -m pytest -q tests/features/test_web_reading.py tests/test_web_policy.py tests/test_architecture.py`：73 项通过。故障注入只在 DNS 边界模拟解析失败/重绑定，其余连接、重定向、正文、超时和取消均为真实本地 ThreadingHTTPServer，不使用 MockTransport。没有迁移或删除已有场景。

Ruff format/lint、Pyright 与全量 pytest 是每项提交门禁。真实互联网搜索、模型选择来源与五题对照尚未运行，不用这些本地测试代替 C01.2～4。
