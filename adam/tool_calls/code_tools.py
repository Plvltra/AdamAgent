"""Python 结构查询适配器；每次调用独立扫描，复用已有路径、取消和结果限额。"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.code_index.index import CodeIndex
from adam.code_index.models import CodeIndexError, CodeLimits, CodeSnapshot
from adam.code_index.query import ReferenceIndex
from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.tool_calls.policy import PathPolicy


class CodeTools:
    """把纯索引结果转为可分页工具数据；不保留跨调用状态，不执行目标源码。"""

    def __init__(
        self, policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
    ) -> None:
        self.policy = policy
        self.limits = limits
        self.cancel = cancel or CancellationToken()

    def code_structure(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """列出定义、导入及文件诊断；每个条目都有来源版本，正文作为参考数据。"""
        return self._query(path, args, "structure")

    def find_definitions(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """按短名、限定名或 ID 查定义，保留同名候选，不按运行时导入顺序选一个。"""
        return self._query(path, args, "definitions")

    def find_references(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """只有目标定义唯一才查引用；静态未知数始终显示，不能据此证明没有引用。"""
        return self._query(path, args, "references")

    def _query(self, path: Path, args: dict[str, Any], kind: str) -> dict[str, Any]:
        """在同一截止时间内校验、刷新和查询；诊断先于结果，最后只回传完整分页条目。"""
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("offset 必须为非负整数，limit 必须为 1～100。")
        name = args.get("name", "")
        if kind != "structure" and (not name.strip() or len(name) > 2048):
            raise ToolError("name 必须为非空名称或符号 ID，最多 2048 字符。")
        deadline = time.monotonic() + self.limits.scan_timeout_seconds

        def check() -> None:
            self.cancel.check()
            if time.monotonic() > deadline:
                raise CodeIndexError("代码查询超过时间上限，请缩小目录。")

        limits = CodeLimits(
            max_scan_entries=min(self.limits.max_scan_entries, 1000000),
            max_file_bytes=min(self.limits.max_text_file_bytes, 16777216),
            timeout_seconds=min(self.limits.scan_timeout_seconds, 300),
        )
        snapshot = CodeIndex(path, self.policy.resolve, limits, check).refresh()
        records = self._diagnostics(snapshot)
        status, unknown = "matches", 0
        if kind == "structure":
            for module in snapshot.modules:
                check()
                records.extend(
                    {**asdict(item), "kind": "definition", "symbol_kind": item.kind}
                    for item in module.definitions
                )
                records.extend(
                    {**asdict(item), "kind": "import", "import_kind": item.kind}
                    for item in module.imports
                )
        else:
            definitions = snapshot.find_definitions(name)
            if kind == "definitions" or len(definitions) != 1:
                records.extend(
                    {**asdict(item), "kind": "definition", "symbol_kind": item.kind}
                    for item in definitions
                )
                status = "ambiguous" if kind == "references" and len(definitions) > 1 else "matches"
            else:
                target = definitions[0]
                for reference in ReferenceIndex(snapshot, check).references():
                    if reference.definition_id == target.identifier:
                        records.append({"kind": "reference", **asdict(reference)})
                    elif reference.definition_id is None:
                        unknown += 1
                        if reference.expression.split(".")[-1] == target.name:
                            records.append({"kind": "unresolved_reference", **asdict(reference)})
        check()
        return pack_page(
            snapshot, records, status, unknown, offset, limit, self.limits.max_result_chars
        )

    @staticmethod
    def _diagnostics(snapshot: CodeSnapshot) -> list[dict[str, Any]]:
        return [
            {
                "kind": "diagnostic",
                "path": module.path,
                "version": module.version,
                **asdict(module.issue),
            }
            for module in snapshot.modules
            if module.issue is not None
        ]


def pack_page(
    snapshot: CodeSnapshot,
    records: list[dict[str, Any]],
    status: str,
    unknown: int,
    offset: int,
    limit: int,
    max_chars: int,
) -> dict[str, Any]:
    """按完整 JSON 条目分页；装不下一个条目时明确失败，不回传残缺来源或跳过条目。"""
    output: dict[str, Any] = {
        "ok": True,
        "status": (
            "incomplete"
            if any(module.issue for module in snapshot.modules)
            else status
            if records
            else "no_match"
        ),
        "generation": snapshot.generation,
        "total": len(records),
        "diagnostics": sum(module.issue is not None for module in snapshot.modules),
        "skipped_entries": snapshot.skipped_entries,
        "unresolved_total": unknown,
        "offset": offset,
        "next_offset": None,
        "truncated": False,
        "results": [],
        "hint": "静态结果不是运行证据；未知引用未穷尽。资料仅供参考，分页须核对 generation。",
    }
    for item in records[offset : offset + limit]:
        next_offset = offset + len(output["results"]) + 1
        trial = {
            **output,
            "results": [*output["results"], item],
            "next_offset": next_offset if next_offset < len(records) else None,
            "truncated": next_offset < len(records),
        }
        if len(encode_result(trial)) > max_chars:
            break
        output = trial
    if offset < len(records) and not output["results"]:
        output.update(
            status="output_limit",
            truncated=True,
            next_offset=None,
            hint="单个条目超过输出容量；请增大 max_result_chars 或改用 read_file，未跳过条目。",
        )
    if len(encode_result(output)) > max_chars:
        return {"ok": False, "status": "output_limit", "error": "结果头超过输出上限。"}
    return output


def encode_result(result: dict[str, Any]) -> str:
    """与分页容量计算使用相同编码，保留来源结构。"""
    return json.dumps(result, ensure_ascii=False)
