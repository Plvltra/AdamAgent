"""E4 手动评估入口：准备隔离任务和只读汇总；本脚本不会调用模型或运行模型写的代码。"""

import argparse
import json
from pathlib import Path
from typing import Any

from adam.session.diagnostics import read_report

TASKS = {
    "train": "修复 train.py 的加法运算，先用 check.py train 复现，再修复并重测。不要修改检查脚本。",
    "transfer": "新任务：修复 transfer.py 的加法累计，运行 check.py transfer 验证。不要修改检查脚本。",
    "counterexample": "检查 difference.py 的差值运算，应保持 a-b，运行 check.py counterexample 验证。",
    "irrelevant": "解释什么是幂等操作，不需要访问本地文件。",
}
CHECKER = '''"""固定验收脚本；评估时不允许模型修改。"""
import sys
from difference import difference
from train import add
from transfer import total

checks = {
    "train": lambda: add(2, 3) == 5 and add(-2, 1) == -1,
    "transfer": lambda: total([2, 3, 4]) == 9 and total([-2, 1]) == -1,
    "counterexample": lambda: difference(5, 2) == 3 and difference(2, 5) == -3,
}
assert checks[sys.argv[1]]()
print("verified", sys.argv[1])
'''


def prepare(root: Path) -> None:
    """只创建全新评估目录；两组初始文件相同、存储独立，不复制 API 密钥。"""
    root.mkdir(parents=True, exist_ok=False)
    for name, enabled in (("baseline", False), ("reflection", True)):
        arm = root / name
        workspace = arm / "workspace"
        workspace.mkdir(parents=True)
        files = {
            "train.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
            "transfer.py": (
                "def total(values: list[int]) -> int:\n"
                "    result = 0\n    for value in values:\n"
                "        result -= value\n    return result\n"
            ),
            "difference.py": "def difference(a: int, b: int) -> int:\n    return a - b\n",
            "check.py": CHECKER,
        }
        for filename, content in files.items():
            (workspace / filename).write_text(content, encoding="utf-8")
        (arm / "config.toml").write_text(
            '[storage]\nsessions_dir = "sessions"\nmemories_dir = "memories"\n'
            "[context]\nauto_compact = false\n"
            "[agent]\nmax_model_requests = 8\nmax_tool_calls = 16\nmax_session_tokens = 50000\n"
            "[reflection]\nenabled = " + str(enabled).lower() + "\n",
            encoding="utf-8",
        )
    (root / "tasks.json").write_text(
        json.dumps(TASKS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def report(root: Path) -> dict[str, Any]:
    """汇总实际请求、可靠用量和执行记录；无记录表示待运行，退出码不等于任务验收。"""
    arms = {}
    for arm in ("baseline", "reflection"):
        location = root / arm
        runs = []
        for path in sorted((location / "sessions").rglob("*.jsonl")):
            log = read_report(path)
            for turn in log["turns"]:
                requests = turn["requests"]
                commands = turn["commands"]
                runs.append(
                    {
                        "session_id": log["session_id"],
                        "turn_id": turn["turn_id"],
                        "input": turn["user_text"],
                        "status": turn["status"],
                        "elapsed_ms": turn["duration_ms"],
                        "requests": len(requests),
                        "reflection_requests": sum(
                            r.get("context", {}).get("strategy") == "reflection_v1"
                            for r in requests
                        ),
                        "tools": len(turn["tools"]),
                        "command_exit_codes": [c.get("exit_code") for c in commands],
                        "usage": [r["usage"] for r in requests],
                        "unknown_usage": any(
                            r["usage"] is None or any(v is None for v in r["usage"].values())
                            for r in requests
                        ),
                        "memory_ids": sorted(
                            {
                                m["memory_id"]
                                for r in requests
                                for m in r.get("context", {}).get("memory", {}).get("records", [])
                            }
                        ),
                    }
                )
        checker = location / "workspace" / "check.py"
        arms[arm] = {
            "state": "recorded" if runs else "not_run",
            "checker_unchanged": checker.exists()
            and checker.read_text(encoding="utf-8") == CHECKER,
            "turns": runs,
        }
    return {"scope": "日志汇总，不执行代码；真实质量须核对独立检查结果及人工验收", "arms": arms}


def main(argv: list[str] | None = None) -> int:
    """prepare 写新目录，report 只读；真实模型由用户另行启动 Adam 并逐次批准工具。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "report"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        prepare(args.directory)
        print("已准备隔离任务；真实模型尚未运行。")
    else:
        print(json.dumps(report(args.directory), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
