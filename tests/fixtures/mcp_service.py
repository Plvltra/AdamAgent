"""真实 stdio 验收服务；故障仅在测试显式调用时制造，stdout 留给协议。"""

import argparse
import os
from pathlib import Path

import anyio
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("Adam test", log_level="ERROR")
args = argparse.ArgumentParser()
args.add_argument("--pid-file", type=Path)
options = args.parse_args()
if options.pid_file:
    options.pid_file.write_text(str(os.getpid()), encoding="utf-8")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def probe(mode: str = "ok", data: dict[str, int] | None = None) -> dict[str, object]:
    """测试握手、嵌套参数、工具失败、超时和断开。"""
    if options.pid_file:
        with options.pid_file.with_suffix(".calls").open("a", encoding="utf-8") as log:
            log.write(mode + "\n")
    if mode == "slow":
        await anyio.sleep(30)
    if mode == "disconnect":
        os._exit(7)
    if mode == "error":
        raise ValueError("expected test failure")
    return {"text": "pong", "pid": os.getpid(), "data": data}


@server.tool()
def unsafe() -> str:
    """未声明只读，用于验证客户端不会暴露它。"""
    return "must not be registered"


if __name__ == "__main__":
    server.run()
