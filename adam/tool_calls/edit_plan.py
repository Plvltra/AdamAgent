"""从快照或明确缺失状态生成单文件计划；不访问文件，也不执行修改。"""

from difflib import unified_diff
from pathlib import Path

from adam.contracts.checkpoints import EditCheckpoint
from adam.contracts.errors import ToolError
from adam.contracts.models import CreatePlan, DeletePlan
from adam.contracts.models import EditPlan as EditPlan
from adam.tool_calls.file_evidence import FileStateError
from adam.tool_calls.text_file import TextSnapshot


class EditConflictError(FileStateError):
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
        raise EditConflictError("文件已变化，请重新读取后提出修改。", version=snapshot.version)
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
    return EditPlan(
        snapshot.path,
        snapshot.version,
        candidate,
        _diff(snapshot.path, text, changed),
        snapshot.raw,
    )


def plan_create(path: Path, content: str, max_bytes: int) -> CreatePlan:
    """生成UTF-8/LF创建候选与完整diff；支持空文件，拒绝孤立CR、NUL或超限。

    path须由宿主检查写入权限、现有父目录与目标缺失；此纯函数不以磁盘查询替代这些检查。
    新建没有旧内容，before_version与original均为None，不能当作已有空文件的替换。
    """
    if not path.is_absolute() or type(max_bytes) is not int or max_bytes < 1:
        raise ToolError("创建计划需要绝对路径和有效文件大小上限。")
    text = _normalize(content)
    try:
        candidate = text.encode("utf-8")
    except UnicodeError as error:
        raise ToolError("新文件内容不是有效UTF-8。") from error
    if len(candidate) > max_bytes:
        raise ToolError("新文件内容超过文本大小上限。")
    return CreatePlan(path, candidate, _diff(path, "", text, creating=True))


def plan_span_edit(
    snapshot: TextSnapshot,
    start_text: str,
    end_text: str,
    new_text: str,
    expected_version: str,
    max_bytes: int,
) -> EditPlan:
    """用两个唯一锚点定位替换段，包含首尾锚点，输出原EditPlan；不读写磁盘。

    锚点各自必须非空、最多4096字符，并在LF归一化全文中恰好出现一次且不重叠。
    版本不符先报冲突；逆序、歧义、无变化、混合换行或候选超限均拒绝。
    new_text为空表示删除选中段；BOM、原换行及完整差异仍由plan_edit处理。
    """
    if snapshot.version != expected_version:
        raise EditConflictError("文件已变化，请重新读取后提出修改。", version=snapshot.version)
    text = _normalize(snapshot.text)
    start, end = _normalize(start_text), _normalize(end_text)
    first = _unique_anchor(text, start, "首")
    last = _unique_anchor(text, end, "尾")
    if last < first + len(start):
        raise ToolError("首尾锚点不能逆序或重叠，请使用相互独立的边界。")
    # 只将宿主已读取的完整选中段交给原规划器；模型不必重复输出数百行旧内容。
    old_text = text[first : last + len(end)]
    return plan_edit(snapshot, old_text, new_text, expected_version, max_bytes)


def plan_restore(snapshot: TextSnapshot, checkpoint: EditCheckpoint) -> EditPlan | DeletePlan:
    """从当前版本与检查点生成恢复字节或删除计划；路径/版本不符时拒绝更改外部修改。"""
    if snapshot.path != checkpoint.path or snapshot.version != checkpoint.target_version:
        raise EditConflictError(
            "文件与该次修改后的版本不一致，不能撤销；请先核对外部改动。", version=snapshot.version
        )
    if checkpoint.original is None:
        return DeletePlan(
            snapshot.path,
            snapshot.version,
            _diff(snapshot.path, _normalize(snapshot.text), "", deleting=True),
            snapshot.raw,
        )
    try:
        original = checkpoint.original.decode("utf-8-sig")
    except UnicodeError as error:
        raise ToolError("检查点不是可恢复的 UTF-8 文本。") from error
    return EditPlan(
        snapshot.path,
        snapshot.version,
        checkpoint.original,
        _diff(snapshot.path, _normalize(snapshot.text), _normalize(original)),
        snapshot.raw,
    )


def _unique_anchor(text: str, anchor: str, label: str) -> int:
    """按精确子串匹配并检测重叠出现，不猜测模型想选哪一处。"""
    if not anchor or len(anchor) > 4096:
        raise ToolError(f"{label}锚点必须为1～4096字符。")
    index = text.find(anchor)
    if index < 0 or text.find(anchor, index + 1) >= 0:
        raise ToolError(f"{label}锚点必须恰好匹配一处，请增加边界上下文。")
    return index


def _diff(
    path: Path, before: str, after: str, *, creating: bool = False, deleting: bool = False
) -> str:
    """为编辑、创建和撤销生成完整差异；展示不重新编码实际写入字节。

    创建/删除以/dev/null标识缺失的一侧；空文件也有明确头信息，避免将缺失与空内容混淆。
    """
    if creating and not after:
        return f"new empty file\n--- /dev/null\n+++ {path}\n"
    if deleting and not before:
        return f"delete empty file\n--- {path}\n+++ /dev/null\n"
    lines = unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="/dev/null" if creating else str(path),
        tofile="/dev/null" if deleting else str(path),
    )
    # 没有末尾换行的删除/新增行也要独立展示，避免相邻 diff 行粘连。
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def _normalize(text: str) -> str:
    """工具参数允许 LF 或 CRLF；拒绝孤立 CR 和二进制空字符。"""
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\0" in text:
        raise ToolError("不支持孤立 CR 或二进制空字符。")
    return text
