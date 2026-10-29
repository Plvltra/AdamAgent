"""从读取快照生成单文件替换计划；不访问文件，也不执行修改。"""

from difflib import unified_diff

from adam.contracts.errors import ToolError
from adam.contracts.models import EditPlan as EditPlan
from adam.tool_calls.text_file import TextSnapshot


class EditConflictError(ToolError):
    """读取版本已经失效，必须重新读取后规划。"""


def plan_edit(
    snapshot: TextSnapshot,
    old_text: str,
    new_text: str,
    expected_version: str,
    max_bytes: int,
) -> EditPlan:
    """返回唯一匹配的替换计划，保留 BOM 和统一的原换行形式。

    expected_version 必须来自读取工具；版本变化、零/多匹配、混合换行、
    无变化或候选内容过大均抛 ToolError，不产生文件副作用。
    """
    if snapshot.version != expected_version:
        raise EditConflictError("文件已变化，请重新读取后提出修改。")
    text = _normalize(snapshot.text)
    has_crlf = "\r\n" in snapshot.text
    if has_crlf and "\n" in snapshot.text.replace("\r\n", ""):
        raise ToolError("暂不编辑混合换行文件。")
    old, new = _normalize(old_text), _normalize(new_text)
    if not old:
        raise ToolError("旧文本不能为空。")
    start = text.find(old)
    if start < 0 or text.find(old, start + 1) >= 0:
        raise ToolError("旧文本必须恰好匹配一处，请提供更多上下文。")
    changed = text[:start] + new + text[start + len(old) :]
    if changed == text:
        raise ToolError("替换未产生内容变化。")
    encoded = changed.replace("\n", "\r\n") if has_crlf else changed
    candidate = encoded.encode("utf-8")
    if snapshot.raw.startswith(b"\xef\xbb\xbf"):
        candidate = b"\xef\xbb\xbf" + candidate
    if len(candidate) > max_bytes:
        raise ToolError("修改后的文件超过文本大小上限。")
    lines = unified_diff(
        text.splitlines(keepends=True),
        changed.splitlines(keepends=True),
        fromfile=str(snapshot.path),
        tofile=str(snapshot.path),
    )
    # 没有末尾换行的删除/新增行也要独立展示，避免相邻 diff 行粘连。
    diff = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )
    return EditPlan(snapshot.path, snapshot.version, candidate, diff)


def _normalize(text: str) -> str:
    """工具参数允许 LF 或 CRLF；拒绝孤立 CR 和二进制空字符。"""
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\0" in text:
        raise ToolError("不支持孤立 CR 或二进制空字符。")
    return text
