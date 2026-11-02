"""按轮提供工具说明及发现入口；目录查询不执行底层工具，权限仍在原执行器校验。"""

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.embedding import EmbeddingBackend
from adam.contracts.errors import ToolError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode
from adam.contracts.models import ExecutionRecorder
from adam.contracts.tool_catalog import ToolLoadingMode
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery import ToolDirectory, ToolVisibility
from adam.tool_calls.registry import ToolRegistry
from adam.tool_calls.semantic_discovery import SemanticToolRanker

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
        *,
        embedding_backend: EmbeddingBackend | None = None,
    ) -> None:
        self.directory = directory
        self.loading = ToolLoadingMode(config.mode)
        self.mode_provider, self.max_result_chars = mode_provider, max_result_chars
        self.semantic = config.ranking == "semantic"
        self.ranker = SemanticToolRanker(config, embedding_backend)
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
        specs = _specs(self.semantic)
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
        """返回完整候选页及实际排序来源；候选不选中 Schema，放不下一项时明确失败。

        语义编码只在真实查询时发生，不在预览或组装时加载；保留原分页和输出上限。
        """
        self._active()
        group, query = args.get("group"), args.get("query", "")
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        if len(query) > 200 or offset < 0 or not 1 <= limit <= 100:
            raise ToolError("工具目录查询或分页范围无效。")
        ranked = self.ranker.rank(self.directory, self.mode_provider(), query, group=group)
        scores = dict(ranked.scores)
        matches = [
            {
                "name": entry.name,
                "group": entry.group,
                "summary": entry.summary,
                **({"score": scores[entry.name]} if entry.name in scores else {}),
            }
            for entry in ranked.entries
        ]
        provenance = (
            {
                "ranking": {
                    "strategy": ranked.strategy,
                    "directory_version": ranked.directory_version,
                    "embedding": ranked.embedding,
                    "fallback_reason": ranked.fallback_reason,
                }
            }
            if self.semantic
            else {}
        )
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
                **provenance,
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
    *,
    embedding_backend: EmbeddingBackend | None = None,
) -> tuple[ToolRegistry, ToolDiscovery]:
    """冻结可信目录后添加发现入口；语义后端由宿主提供，未提供时查询明确降级。

    无效外部说明以受控错误拒绝；构造和预览不推理、不下载、不选择底层工具。
    """
    try:
        directory = registry.directory()
    except ValueError as error:
        raise ToolError("工具目录说明无效，未启动模型请求。") from error
    service = ToolDiscovery(
        directory,
        config,
        mode_provider,
        registry.limits.max_result_chars,
        embedding_backend=embedding_backend,
    )
    registered = registry.extended(
        _specs(service.semantic),
        {"list_tools": service.list_tools, "discover_tools": service.discover},
        capabilities={
            "list_tools": ToolCapability.READ_ONLY,
            "discover_tools": ToolCapability.SESSION_STATE,
        },
        groups=dict.fromkeys(_SPECS, "discovery"),
    )
    return registered, service


def _specs(semantic: bool) -> dict[str, Any]:
    """注册与请求使用相同说明；默认关键词模式的既有 Schema 保持。"""
    result = deepcopy(_SPECS)
    if semantic:
        result["list_tools"]["description"] = (
            "按任务描述语义检索当前模式允许的工具，或按准确分组分页查询。"
            "返回候选、排序来源及可能的关键词降级原因；分数不是执行权限。"
            "查询不选择也不执行工具，须 discover_tools 后在后续请求调用。"
        )
        result["list_tools"]["parameters"]["properties"]["query"]["description"] = (
            "自然语言任务描述或准确工具名，最多200字符；空字符串按目录顺序列出"
        )
    return result
