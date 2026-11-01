"""显式运行真实隔离脚本和注册文件工具；不使用模型，不用替身证明 OS 隔离。"""

import argparse
import json
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import get_ident
from time import monotonic

from adam.isolation.backend import IsolationError, WslBubblewrap
from adam.isolation.rpc import IsolatedOrchestrator
from adam.tool_calls.code_bridge import RegisteredToolBridge
from adam.tool_calls.factory import create_tool_registry


def verify(*, backpressure: bool = False) -> dict[str, object]:
    """临时文件留在宿主；隔离脚本必须经注册工具获得内容，再自行筛选汇总。"""
    with tempfile.TemporaryDirectory(prefix="adam-rpc-") as directory:
        root = Path(directory)
        (root / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        registry = create_tool_registry(root)
        bridge = RegisteredToolBridge(registry, ("file_info", "read_file"))
        orchestrator = IsolatedOrchestrator(WslBubblewrap())
        coordinator = get_ident()
        calls = []

        def invoke(name: str, arguments: dict) -> dict:
            calls.append((name, get_ident()))
            return bridge(name, arguments)

        result = orchestrator.run(
            "info = call_tool('file_info', {'path': 'notes.txt'})\n"
            "text = call_tool('read_file', {'path': 'notes.txt'})\n"
            "result = {'size': info['size'], 'selected': [row['text'] for row in text['lines'] if 'mm' in row['text']]}",
            invoke,
        )
        if (
            not result.has_result
            or result.calls != 2
            or result.result != {"size": 20, "selected": ["gamma"]}
        ):
            raise RuntimeError("真实 RPC 验收未通过。")
        assert calls == [("file_info", coordinator), ("read_file", coordinator)]
        denied = orchestrator.run(
            "result = call_tool('run_command', {'argv': ['git', '--version']})",
            invoke,
        )
        assert denied.result["ok"] is False and denied.calls == 1
        direct = orchestrator.run(
            "import os, socket\n"
            "result = {'host_visible': os.path.exists('/mnt/c')}\n"
            "try:\n    socket.socket()\nexcept PermissionError:\n    result['network_denied'] = True\n",
            invoke,
        )
        assert (
            direct.result == {"host_visible": False, "network_denied": True} and direct.calls == 0
        )
        assert (root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
        report: dict[str, object] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "real_backend": True,
            "coordinator_thread_preserved": True,
            "results": {
                "pipeline": asdict(result),
                "unexposed_tool": asdict(denied),
                "direct_access": asdict(direct),
            },
        }
        if backpressure:
            report["backpressure"] = verify_stalled_reply(orchestrator)
        return report


def verify_stalled_reply(orchestrator: IsolatedOrchestrator) -> dict[str, object]:
    """worker 故意不读两份大回复；从首次回调起计时，排除 WSL 冷启动成本。"""
    code = (
        "import json, sys, time\n"
        "for sequence in (1, 2):\n"
        "    sys.__stdout__.write(json.dumps({'kind':'call', 'sequence':sequence, 'name':'synthetic', 'arguments':{}}) + '\\n')\n"
        "    sys.__stdout__.flush()\n"
        "    time.sleep(0.2)\n"
        "time.sleep(90)\n"
    )
    calls: list[float] = []

    def reply(name: str, arguments: dict) -> dict:
        calls.append(monotonic())
        return {"value": "x" * 60000}

    try:
        orchestrator.run(code, reply)
    except IsolationError:
        elapsed = monotonic() - calls[0] if calls else 0
        if len(calls) not in (1, 2) or elapsed >= 75:
            raise RuntimeError("真实回复阻塞未在上限内停止。") from None
        return {"stopped": True, "calls": len(calls), "elapsed_seconds": round(elapsed, 2)}
    raise RuntimeError("故意不读取回复的 worker 不应成功。")


def main() -> int:
    """真实检查全部通过后排他保存报告；不保存宿主临时路径或原始中间工具结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--backpressure", action="store_true", help="另做约 60 秒的真实不读回复检查"
    )
    args = parser.parse_args()
    report = verify(backpressure=args.backpressure)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("真实隔离 RPC 场景通过，工具回调保持在协调线程；阻塞检查状态见报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
