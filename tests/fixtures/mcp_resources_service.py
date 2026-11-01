"""真实 SDK resources 服务；故障由固定测试参数选择，stdio 只承载 MCP。"""

import argparse
import asyncio
import os
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    BlobResourceContents,
    ListResourcesResult,
    ReadResourceResult,
    Resource,
    TextResourceContents,
)

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--scenario", default="normal")
args = parser.parse_args()
(args.root / "server.pid").write_text(str(os.getpid()), encoding="utf-8")


def record(value):
    with (args.root / "server.calls").open("a", encoding="utf-8") as stream:
        stream.write(value + "\n")


async def list_resources(ctx, params):
    cursor = params.cursor if params else None
    record("list:" + str(cursor))
    resource = Resource(uri="docs://note", name="note", mime_type="text/plain")
    scenario = args.scenario
    if scenario == "declared_large":
        resource.size = 1000000
    if scenario == "metadata":
        resource.description = "x" * 2001
    if scenario == "duplicate":
        return ListResourcesResult(resources=[resource, resource])
    if scenario == "missing":
        return ListResourcesResult(resources=[])
    if scenario == "count":
        return ListResourcesResult(
            resources=[Resource(uri=f"docs://item/{i}", name=str(i)) for i in range(65)]
        )
    if scenario == "pages":
        number = int(cursor or "0")
        return ListResourcesResult(
            resources=[Resource(uri=f"docs://item/{number}", name=str(number))],
            next_cursor=str(number + 1),
        )
    if scenario == "loop":
        return ListResourcesResult(resources=[], next_cursor="loop")
    if cursor:
        return ListResourcesResult(resources=[Resource(uri="docs://hidden", name="hidden")])
    return ListResourcesResult(resources=[resource], next_cursor="second")


async def read_resource(ctx, params):
    record("read:" + params.uri)
    scenario = args.scenario
    if scenario == "slow":
        await asyncio.sleep(30)
    if scenario == "disconnect":
        os._exit(7)
    if scenario == "binary":
        return ReadResourceResult(
            contents=[BlobResourceContents(uri=params.uri, mime_type="image/png", blob="AA==")]
        )
    text = (args.root / "note.txt").read_text(encoding="utf-8")
    if scenario == "large":
        text = "中" * 22000
    content = TextResourceContents(
        uri="docs://hidden" if scenario == "different" else params.uri,
        text=text,
        mime_type="text/plain",
    )
    return ReadResourceResult(contents=[content] * (9 if scenario == "many" else 1))


async def main():
    server = Server(
        "Adam resource fixture", on_list_resources=list_resources, on_read_resource=read_resource
    )
    async with stdio_server() as (incoming, outgoing):
        await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
