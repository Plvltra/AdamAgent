"""后置 Hook 示例：只解析已成功修改的 Python 文件，不导入或运行目标代码。"""

import ast
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any


def inspect_python(data: dict[str, Any]) -> str:
    """核对实际文件版本后解析语法；不适用的工具返回跳过说明，错误交由主入口报告。"""
    if (
        type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("event") != "after_tool"
    ):
        raise ValueError("requires after_tool version 1")
    tool, result = data["tool"], data["result"]
    if tool["name"] not in {"replace_text", "apply_proposal"} or result.get("status") != "applied":
        return "未发生专用文件修改，跳过 Python 语法检查"
    workspace = Path(data["workspace"]).resolve()
    target = Path(result["path"]).resolve()
    if target.suffix.lower() != ".py":
        return "非 Python 文件，跳过语法检查"
    if not target.is_relative_to(workspace):
        raise ValueError("target is outside workspace")
    with target.open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576 or sha256(raw).hexdigest() != result.get("after_version"):
        raise ValueError("file too large or version changed")
    ast.parse(raw, filename=str(target))
    return "已修改文件的 Python 语法检查通过；未执行代码，不代表行为测试通过"


def main() -> int:
    """检查失败返回非零和诊断；宿主保留已完成编辑，不能将此错误当作编辑未执行。"""
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("input too large")
        note = inspect_python(json.loads(raw))
    except SyntaxError as error:
        print(f"Python 语法检查失败，行号 {error.lineno}；文件修改已经发生。", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError, RecursionError):
        print("无法核对已修改文件的版本或读取并解析文件；修改没有回滚。", file=sys.stderr)
        return 2
    print(json.dumps({"version": 1, "note": note}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
