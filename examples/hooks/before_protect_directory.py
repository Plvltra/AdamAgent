"""前置 Hook 示例：限制专用工具修改或创建指定目录内的文件，不替代命令审批或文件权限。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def decide(data: dict[str, Any], protected: list[str]) -> dict[str, Any]:
    """按解析后的工作目录和路径判断；其他工具继续由各自权限与审批控制。"""
    if (
        type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("event") != "before_tool"
    ):
        raise ValueError("requires before_tool version 1")
    tool = data["tool"]
    if tool["name"] not in {"replace_text", "replace_span", "apply_proposal", "create_file"}:
        return {"version": 1, "decision": "allow", "reason": "此示例只检查专用文件编辑"}
    workspace = Path(data["workspace"]).resolve()
    target = (workspace / tool["arguments"]["path"]).resolve()
    blocked = any(
        target.is_relative_to((workspace / directory).resolve()) for directory in protected
    )
    return {
        "version": 1,
        "decision": "deny" if blocked else "allow",
        "reason": "目标位于禁止专用编辑的目录" if blocked else "目录检查通过，仍需原有权限与批准",
    }


def main() -> int:
    """从 stdin 读取一个有界事件；异常退出使宿主阻止工具，不输出假允许。"""
    parser = argparse.ArgumentParser(
        description="Restrict dedicated file edits in selected directories"
    )
    parser.add_argument("directories", nargs="+", help="Directories relative to the tool workspace")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("input too large")
        result = decide(json.loads(raw), args.directories)
    except (ValueError, KeyError, TypeError, OSError):
        print("无法完成目录检查，请检查输入和目录配置。", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
