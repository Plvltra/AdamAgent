"""对照直接函数与复用 MCP 连接的同一只读查询；不调用 LLM。"""

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

from adam.config.mcp import MCPServerConfig
from adam.mcp.client import MCPConnection
from examples.mcp_readonly import read_text


def compare(workspace: Path, path: str, runs: int = 3) -> dict[str, object]:
    """分别测冷启动、直接查询和复用连接；结果一致才返回对照数据。"""
    if not 1 <= runs <= 20:
        raise ValueError("runs must be between 1 and 20")
    server = Path(__file__).resolve().parents[1] / "examples" / "mcp_readonly.py"
    started = perf_counter()
    client = MCPConnection(
        MCPServerConfig(
            "files",
            sys.executable,
            (str(server), "--workspace", str(workspace.resolve())),
            ("read_project_text",),
        ),
        workspace,
    ).open()
    startup = perf_counter() - started
    local_seconds, remote_seconds = 0.0, 0.0
    local, remote = {}, {}
    try:
        for _ in range(runs):
            started = perf_counter()
            local = read_text(workspace, path)
            local_seconds += perf_counter() - started
            started = perf_counter()
            remote = client.call("read_project_text", {"path": path})
            remote_seconds += perf_counter() - started
            if not remote["ok"] or remote["structured_content"] != local:
                raise ValueError("direct and protocol results differ")
    finally:
        client.close()
    return {
        "runs": runs,
        "same_result": True,
        "cold_start_seconds": startup,
        "direct_mean_seconds": local_seconds / runs,
        "mcp_mean_seconds": remote_seconds / runs,
        "direct_result_chars": len(json.dumps(local, ensure_ascii=False)),
        "mcp_result_chars": len(json.dumps(remote, ensure_ascii=False)),
    }


def main() -> None:
    """从项目根用 python -m scripts.compare_mcp 运行，输出实际测量值。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(compare(args.workspace, args.path, args.runs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
