"""本机回环上的真实 MCP HTTP 服务；认证只记录匹配布尔值，不落盘令牌。"""

import argparse
import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--scenario", default="normal")
args = parser.parse_args()
server = MCPServer("Adam HTTP fixture", log_level="ERROR")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def ping(text: str) -> str:
    """只回显测试文本；slow 故障发生在服务器已接收请求之后。"""
    if args.scenario == "slow":
        await asyncio.sleep(30)
    return "pong:" + text


@server.resource("docs://hello", mime_type="text/plain")
def hello():
    return "HTTP resource"


@server.prompt()
def review(text: str):
    return "Review: " + text


class Gate:
    """在真实 SDK 应用前制造 HTTP 错误；不自行实现 MCP 协议。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/health":
            return await self.respond(send, 200, b"ready")
        headers = dict(scope["headers"])
        authorized = headers.get(b"authorization") == b"Bearer test-mcp-bearer-123"
        raw = b""
        if scope["method"] == "POST":
            while True:
                part = await receive()
                raw += part.get("body", b"")
                if not part.get("more_body"):
                    break
        value = json.loads(raw) if raw else {}
        with (args.root / "http.calls").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "method": scope["method"],
                        "path": scope["path"],
                        "rpc": value.get("method"),
                        "authorized": authorized,
                    }
                )
                + "\n"
            )
        if args.scenario == "auth" and not authorized:
            return await self.respond(send, 401, b"authentication required")
        if args.scenario in {"redirect", "cross_redirect"}:
            location = (
                b"/elsewhere" if args.scenario == "redirect" else b"http://127.0.0.1:1/stolen"
            )
            return await self.respond(send, 307, b"redirect", [(b"location", location)])
        if value.get("method") == "tools/call":
            if args.scenario == "error":
                return await self.respond(send, 503, b"temporary failure")
            if args.scenario == "malformed":
                return await self.respond(
                    send, 200, b"broken json", [(b"content-type", b"application/json")]
                )

        sent = False

        async def buffered():
            nonlocal sent
            if not sent and scope["method"] == "POST":
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, buffered, send)

    @staticmethod
    async def respond(send, status, body, headers=None):
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": body})


def tls_files():
    """只为本地 TLS 拒绝验收生成短期自签名证书；不读真实证书或改变系统信任。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Adam ephemeral test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    (args.root / "test.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (args.root / "test.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {"ssl_certfile": str(args.root / "test.pem"), "ssl_keyfile": str(args.root / "test.key")}


if __name__ == "__main__":
    # 由子进程自己绑定随机回环端口，避免先选端口再启动之间的竞争。
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    tls = tls_files() if args.scenario == "tls" else {}
    scheme = "https" if tls else "http"
    (args.root / "url.txt").write_text(f"{scheme}://127.0.0.1:{port}/mcp", encoding="utf-8")
    application = server.streamable_http_app(json_response=args.scenario != "sse")
    uvicorn.Server(uvicorn.Config(Gate(application), log_level="error", **tls)).run(sockets=[sock])
