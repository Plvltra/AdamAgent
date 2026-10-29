"""独立本地只读 MCP 教学服务；以绝对脚本路径启动，stdout 仅用于协议。"""

import argparse
from hashlib import sha256
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations


def read_text(root: Path, path: str) -> dict[str, str]:
    """读取根内有界 UTF-8 文本；同时拒绝词法越界、链接逃逸与凭据文件。"""
    base = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    target = candidate.resolve()
    for value in (candidate.absolute(), target):
        if ".." in value.parts or not value.is_relative_to(base):
            raise ValueError("path outside workspace")
        if any(p.lower() in {".ssh", ".aws", ".git"} for p in value.parts):
            raise ValueError("protected directory")
        if (
            value.name.lower().startswith(".env")
            or value.name.lower() in {"llm.toml", "id_rsa", "id_ed25519"}
            or value.suffix.lower() in {".pem", ".key"}
        ):
            raise ValueError("credential file")
    if not target.is_file():
        raise ValueError("not a regular file")
    with target.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("file exceeds byte limit")
    text = raw.decode("utf-8-sig")
    if "\0" in text or len(text) > 3000:
        raise ValueError("not bounded text")
    return {"content": text, "version": sha256(raw).hexdigest()}


def main() -> None:
    """服务启动范围来自用户配置，模型调用不能改根目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace.resolve(strict=True)
    if not root.is_dir():
        parser.error("workspace must be a directory")
    server = MCPServer("Adam readonly text", log_level="ERROR")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def read_project_text(path: str) -> dict[str, str]:
        """读取本服务工作目录内的短 UTF-8 文件，返回文本与 SHA-256。"""
        return read_text(root, path)

    server.run()


if __name__ == "__main__":
    main()
