"""按轮提供工具说明及发现入口；目录查询不执行底层工具，权限仍在原执行器校验。"""

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.errors import ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode
from adam.contracts.models import ExecutionRecorder
from adam.contracts.tool_catalog import ToolLoadingMode
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery import ToolDirectory, ToolVisibility
from adam.tool_calls.registry import ToolRegistry

_SPECS = {
    "list_tools": schema(
        "按分组或关键词分页查工具名称和用途，不执行、不启用工具。"
        "名称已知或整组需要时可直接 discover_tools。",
        {
            "group": ("string", "可选的准确分组名"),
            "query": ("string", "名称或用途关键词，最多200字符"),
            "offset": ("integer", "从0开始，沿next_offset继续"),
            "limit": ("integer", "1～100项，默认20；结果还受单次输出上限约束"),
        },
        [],
    ),
    "discover_tools": {
        "description": (
            "为本轮后续模型请求选择完整工具说明，可按准确名称或整组选择。"
            "本次返回调用只能使用本次请求已提供的工具；同批发现不能立即调用新工具。"
            "重复选择不重复添加；每轮重新选择，历史出现过的名称也须本轮发现。"
            "发现不执行工具、不改变权限；一般知识问题无需调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "groups": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}


class ToolDiscovery:
    """拥有本执行器的单轮选择；主任务与各子任务必须分别创建实例。"""

    def __init__(
        self,
        directory: ToolDirectory,
        config: ToolDiscoveryConfig,
        mode_provider: Callable[[], ExecutionMode],
        max_result_chars: int,
    ) -> None:
        self.directory = directory
        self.loading = ToolLoadingMode(config.mode)
        self.mode_provider, self.max_result_chars = mode_provider, max_result_chars
        self._turn_id: str | None = None
        self._selection: ToolVisibility | None = None

    def definitions(self, turn_id: str | None = None) -> list[dict[str, Any]]:
        """真实轮次按 ID 重置；None 只预览下一轮初始说明，不改写当前选择。"""
        if turn_id is None:
            selected = self._initial()
        else:
            if turn_id != self._turn_id:
                self._selection = self._initial()
                self._turn_id = turn_id
            assert self._selection is not None
            selected = self._selection
        definitions = selected.snapshot().definitions()
        if self.loading is ToolLoadingMode.FULL:
            return definitions
        specs = deepcopy(_SPECS)
        specs["discover_tools"]["description"] += (
            "\n可发现分组（count为工具数，examples仅为部分名称；完整目录用list_tools）："
            + json.dumps(self._overview(selected), ensure_ascii=False)
        )
        return [
            {"type": "function", "function": {"name": name, **spec}} for name, spec in specs.items()
        ] + definitions

    def require_provided(self, name: str, provided: frozenset[str]) -> None:
        """保留模式拒绝原因，再检查实际请求名单；不执行路径检查、Hook 或动作。"""
        if name not in provided:
            capability = next(
                (
                    entry.capability
                    for entry in self.directory.listings(ExecutionMode.ACT)
                    if entry.name == name
                ),
                ToolCapability.UNKNOWN,
            )
            check_tool_mode(self.mode_provider(), capability)
            raise ToolError("本次请求尚未提供该工具的完整说明；请先发现，再在后续请求调用。")

    def discover(
        self,
        args: dict[str, Any],
        _record: ExecutionRecorder | None,
    ) -> dict[str, Any]:
        """扩展下一次说明；只回传数量，避免把完整 Schema 重复放进工具结果。"""
        selected = self._active()
        added = selected.discover(
            names=tuple(args.get("names", [])),
            groups=tuple(args.get("groups", [])),
        )
        return {
            "status": "selected",
            "added_count": len(added),
            "selected_count": len(selected.snapshot().names),
            "hint": "完整参数会在下一次请求提供；本次调用批次的可用工具保持不变。",
        }

    def list_tools(
        self,
        args: dict[str, Any],
        _record: ExecutionRecorder | None,
    ) -> dict[str, Any]:
        """返回完整条目页；结果放不下一项时明确失败，不用通用截断隐藏分页位置。"""
        entries = self._active().listings()
        group, query = args.get("group"), args.get("query", "").casefold()
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        if group is not None and group not in {entry.group for entry in entries}:
            raise ToolError("未知或当前模式不可用的工具分组。")
        if len(query) > 200 or offset < 0 or not 1 <= limit <= 100:
            raise ToolError("工具目录查询或分页范围无效。")
        matches = [
            {"name": entry.name, "group": entry.group, "summary": entry.summary}
            for entry in entries
            if (group is None or entry.group == group)
            and query in (entry.name + " " + entry.group + " " + entry.summary).casefold()
        ]
        if offset > len(matches):
            raise ToolError("工具目录偏移超出结果范围。")
        page = matches[offset : offset + limit]
        while True:
            end = offset + len(page)
            result = {
                "tools": page,
                "total": len(matches),
                "offset": offset,
                "next_offset": end if end < len(matches) else None,
            }
            if len(json.dumps({"ok": True, **result}, ensure_ascii=False)) <= self.max_result_chars:
                return result
            if len(page) <= 1:
                raise ToolError(
                    "单个工具目录条目超过结果上限，请提高 tool_limits.max_result_chars。"
                )
            page.pop()

    def _initial(self) -> ToolVisibility:
        return ToolVisibility(self.directory, self.mode_provider(), self.loading)

    def _active(self) -> ToolVisibility:
        if self._selection is None:
            raise ToolError("工具发现尚未绑定真实轮次；预览不允许改变选择。")
        return self._selection

    @staticmethod
    def _overview(selected: ToolVisibility) -> list[dict[str, Any]]:
        """每组最多展示三个名称；大量 MCP 工具的摘要留在分页目录。"""
        groups: dict[str, dict[str, Any]] = {}
        for entry in selected.listings():
            group = groups.setdefault(
                entry.group, {"group": entry.group, "count": 0, "examples": []}
            )
            group["count"] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(entry.name)
        return list(groups.values())


def register_discovery(
    registry: ToolRegistry,
    config: ToolDiscoveryConfig,
    mode_provider: Callable[[], ExecutionMode],
) -> tuple[ToolRegistry, ToolDiscovery]:
    """冻结已注册的真实目录，再添加发现适配器；无效外部说明以受控错误拒绝。"""
    try:
        directory = registry.directory()
    except ValueError as error:
        raise ToolError("工具目录说明无效，未启动模型请求。") from error
    service = ToolDiscovery(directory, config, mode_provider, registry.limits.max_result_chars)
    registered = registry.extended(
        _SPECS,
        {"list_tools": service.list_tools, "discover_tools": service.discover},
        capabilities={
            "list_tools": ToolCapability.READ_ONLY,
            "discover_tools": ToolCapability.SESSION_STATE,
        },
        groups=dict.fromkeys(_SPECS, "discovery"),
    )
    return registered, service
