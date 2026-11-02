"""结构化后置 Hook 示例：读取已编辑版本并解析语法，不导入或执行目标代码。"""

import ast
import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any


def inspect_python(data: dict[str, Any]) -> dict[str, Any]:
    """返回版本2的检查事实；非适用操作仍用版本1说明，读取不可靠时不声称语法通过。"""
    if (
        type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("event") != "after_tool"
    ):
        raise ValueError("requires after_tool version 1")
    tool, result = data["tool"], data["result"]
    if (
        tool["name"] not in {"replace_text", "apply_proposal", "create_file"}
        or result.get("status") != "applied"
    ):
        return {"version": 1, "note": "未发生专用文件修改，跳过 Python 语法检查"}
    path = result["path"]
    expected = result["after_version"]
    if (
        not isinstance(path, str)
        or not isinstance(expected, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
    ):
        raise ValueError("invalid applied edit")
    workspace, target = Path(data["workspace"]).resolve(), Path(path).resolve()
    if target.suffix.lower() != ".py":
        return {"version": 1, "note": "非 Python 文件，跳过语法检查"}
    if not target.is_relative_to(workspace):
        raise ValueError("target outside workspace")
    report = {
        "checker": "python.ast",
        "path": path,
        "expected_version": expected,
        "checked_version": None,
        "status": "unavailable",
        "items": [],
    }
    note = "未可靠读取完整文件；不能判断语法，原修改没有回滚。"
    try:
        with target.open("rb") as stream:
            raw = stream.read(1048577)
    except OSError:
        return {"version": 2, "note": note, "diagnostics": report}
    if len(raw) > 1048576:
        return {"version": 2, "note": note, "diagnostics": report}
    report["checked_version"] = sha256(raw).hexdigest()
    if report["checked_version"] != expected:
        report["status"] = "stale"
        return {
            "version": 2,
            "note": "文件已变化，未检查另一版本并冒充本次修改。",
            "diagnostics": report,
        }
    try:
        ast.parse(raw, filename=path)
    except SyntaxError as error:
        message = "Python 语法错误：" + " ".join(str(error.msg).split())
        # 错误说明来自解析器，仍清理控制字符并限制长度；不复制源码片段或整段 traceback。
        message = "".join(
            char if ord(char) >= 32 and ord(char) != 127 else " " for char in message
        )[:500]
        report.update(
            status="failed",
            items=[
                {
                    "line": error.lineno or 1,
                    "column": error.offset if error.offset and error.offset > 0 else None,
                    "severity": "error",
                    "code": "SyntaxError",
                    "message": message,
                }
            ],
        )
        note = "已修改文件存在语法错误；修改已经发生，检查没有执行目标代码。"
    else:
        report["status"] = "passed"
        note = "所记录版本的 Python 语法通过；未执行代码，不代表行为测试通过。"
    return {"version": 2, "note": note, "diagnostics": report}


def main() -> int:
    """协议或检查器本身故障使用非零退出；已发现语法错误则正常交付 failed 报告。"""
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("input too large")
        response = inspect_python(json.loads(raw))
    except (ValueError, KeyError, TypeError, OSError, RecursionError):
        print("检查器无法可靠处理输入；原修改没有回滚。", file=sys.stderr)
        return 2
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
