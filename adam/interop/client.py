"""A2A 0.3.0 同步文本子集；仅连接显式 IPv4 回环源。"""

import json
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from adam.interop.models import AgentCard, InteropError, TextResult

MAX_BYTES = 262144


def local_origin(url: str) -> str:
    """拒绝外网、凭据和隐式端口；不解析 DNS 或跟随卡片转向。"""
    try:
        p = urlsplit(url)
        if (
            p.scheme == "http"
            and p.hostname == "127.0.0.1"
            and p.port
            and p.username is None
            and p.password is None
            and p.path in ("", "/")
            and not p.query
            and not p.fragment
        ):
            return f"http://127.0.0.1:{p.port}"
    except ValueError:
        pass
    raise InteropError("只支持显式端口的 http://127.0.0.1 回环源。")


class TextAgentClient:
    """按次创建连接，无自动重试、代理继承或 HTTP 重定向。"""

    def __init__(self, origin: str, *, timeout: float = 10.0) -> None:
        self.origin = local_origin(origin)
        if not 0 < timeout <= 60:
            raise ValueError("timeout must be in (0, 60]")
        self.timeout = timeout

    def discover(self) -> AgentCard:
        """读取指定源的 Agent Card，拒绝更换目标或不支持的文本能力。"""
        data = self._request("GET", "/.well-known/agent-card.json")
        skills = data.get("skills")
        if (
            data.get("protocolVersion") != "0.3.0"
            or data.get("preferredTransport", "JSONRPC") != "JSONRPC"
            or data.get("url") != self.origin + "/"
            or not isinstance(data.get("name"), str)
            or not data["name"]
            or not isinstance(data.get("defaultInputModes"), list)
            or "text/plain" not in data["defaultInputModes"]
            or not isinstance(data.get("defaultOutputModes"), list)
            or "text/plain" not in data["defaultOutputModes"]
            or not isinstance(skills, list)
            or not skills
            or any(not isinstance(s, dict) or not isinstance(s.get("id"), str) for s in skills)
        ):
            raise InteropError("Agent Card 不符合支持的版本、地址或文本能力。")
        return AgentCard(data["name"], data["url"], tuple(s["id"] for s in skills))

    def send(self, text: str, *, message_id: str | None = None) -> TextResult:
        """发送一次文本；断连或超时结果未知，绝不自动重新委派。"""
        if not text or len(text.encode("utf-8")) > 16384:
            raise InteropError("文本必须为 1～16384 字节。")
        message_id = message_id or uuid4().hex
        if len(message_id) > 128:
            raise InteropError("消息 ID 过长。")
        self.discover()
        request_id = uuid4().hex
        data = self._request(
            "POST",
            "/",
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": message_id,
                        "parts": [{"kind": "text", "text": text}],
                    },
                    "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        if data.get("jsonrpc") != "2.0" or data.get("id") != request_id:
            raise InteropError("远端响应关联 ID 或协议无效。")
        if "error" in data:
            raise InteropError("远端返回协议错误；未重试。")
        result = data.get("result")
        if (
            not isinstance(result, dict)
            or result.get("kind") != "message"
            or result.get("role") != "agent"
        ):
            raise InteropError("当前子集仅支持直接返回 Agent 文本消息；任务状态未作为成功。")
        parts, reply_id = result.get("parts"), result.get("messageId")
        if (
            not isinstance(reply_id, str)
            or not reply_id
            or not isinstance(parts, list)
            or not parts
            or any(
                not isinstance(p, dict)
                or p.get("kind") != "text"
                or not isinstance(p.get("text"), str)
                for p in parts
            )
        ):
            raise InteropError("远端消息字段或文本部分无效。")
        return TextResult(reply_id, "\n".join(p["text"] for p in parts))

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """有界读取响应；连接失败保留未知，不泄露远端正文。"""
        try:
            with httpx.Client(
                timeout=self.timeout, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream(method, self.origin + path, json=payload) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise InteropError("远端响应超过字节限制。")
            data = json.loads(body)
        except (httpx.HTTPError, ValueError) as error:
            raise InteropError("远端连接或响应失败，执行结果可能未知；未重试。") from error
        if not isinstance(data, dict):
            raise InteropError("远端响应必须是 JSON 对象。")
        return data
