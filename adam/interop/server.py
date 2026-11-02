"""显式启动的回环文本 Agent；回调只接收文本，不获取本地会话。"""

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

MAX_BYTES = 262144


def create_server(handler: Callable[[str], str], *, port: int = 0) -> HTTPServer:
    """绑定 127.0.0.1，由宿主启动和关闭；仅供本机实验，不记录访问正文。"""

    class RequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            """仅公开固定 Agent Card 路径。"""
            if self.path != "/.well-known/agent-card.json":
                self.send_error(404)
                return
            self._reply(
                {
                    "protocolVersion": "0.3.0",
                    "name": "Adam local text agent",
                    "description": "Explicit text callback without local session access",
                    "url": f"http://127.0.0.1:{server.server_port}/",
                    "version": "0.1.0",
                    "preferredTransport": "JSONRPC",
                    "capabilities": {"streaming": False, "pushNotifications": False},
                    "defaultInputModes": ["text/plain"],
                    "defaultOutputModes": ["text/plain"],
                    "skills": [
                        {
                            "id": "text",
                            "name": "Text task",
                            "description": "Process bounded text",
                            "tags": ["text"],
                        }
                    ],
                }
            )

        def do_POST(self) -> None:  # noqa: N802
            """校验纯文本新消息后调用回调；异常返回失败而非假成功。"""
            if self.path != "/":
                self.send_error(404)
                return
            self.connection.settimeout(10)
            request_id = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES or self.headers.get("Transfer-Encoding"):
                    raise ValueError("length")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("object")
                request_id = data.get("id")
                if (
                    data.get("jsonrpc") != "2.0"
                    or not isinstance(request_id, (str, int))
                    or isinstance(request_id, bool)
                ):
                    raise ValueError("request")
                if data.get("method") != "message/send":
                    self._error(request_id, -32601, "Method not supported")
                    return
                message = data["params"]["message"]
                parts = message["parts"]
                if (
                    message.get("kind") != "message"
                    or message.get("role") != "user"
                    or not isinstance(message.get("messageId"), str)
                    or not message["messageId"]
                    or len(message["messageId"]) > 128
                    or "taskId" in message
                    or "contextId" in message
                    or not isinstance(parts, list)
                    or not parts
                    or any(
                        not isinstance(p, dict)
                        or p.get("kind") != "text"
                        or not isinstance(p.get("text"), str)
                        for p in parts
                    )
                ):
                    raise ValueError("message")
                config = data["params"].get("configuration", {})
                if (
                    not isinstance(config, dict)
                    or "pushNotificationConfig" in config
                    or config.get("blocking", True) is not True
                    or "text/plain" not in config.get("acceptedOutputModes", ["text/plain"])
                ):
                    raise ValueError("unsupported configuration")
                text = "\n".join(p["text"] for p in parts)
                if not 0 < len(text.encode("utf-8")) <= 16384:
                    raise ValueError("text length")
            except (ValueError, KeyError, TypeError, AttributeError):
                self._error(request_id, -32602, "Invalid text request")
                return
            try:
                answer = handler(text)
                if not isinstance(answer, str) or len(answer.encode("utf-8")) > 65536:
                    raise ValueError("invalid handler result")
            except Exception:
                self._error(request_id, -32603, "Text handler failed")
                return
            self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "kind": "message",
                        "role": "agent",
                        "messageId": uuid4().hex,
                        "parts": [{"kind": "text", "text": answer}],
                    },
                }
            )

        def _error(self, request_id: object, code: int, message: str) -> None:
            """仅返回受控错误，不回传异常细节或用户文本。"""
            self._reply(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )

        def _reply(self, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", port), RequestHandler)
    return server
