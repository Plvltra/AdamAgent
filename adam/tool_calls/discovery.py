"""可信工具目录与单轮选择策略；不执行工具、不读文件、不获取新权限。"""

import json
import re
from collections.abc import Collection, Mapping
from hashlib import sha256
from typing import Any

from adam.contracts.errors import ToolError, ToolModeError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode
from adam.contracts.tool_catalog import ToolExposure, ToolListing, ToolLoadingMode


def validate_groups(groups: Mapping[str, str], names: Collection[str]) -> None:
    """注册分组只允许引用本次工具，标签保持简短稳定；不接受模型声明的权限。"""
    if set(groups) - set(names) or any(
        not isinstance(group, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", group) is None
        for group in groups.values()
    ):
        raise ValueError("工具分组必须引用本次注册名称，并使用1～64位字母、数字、下划线或连字符。")


class ToolDirectory:
    """冻结注册时的说明、能力和分组；扩展注册表后需重新取得目录。"""

    def __init__(
        self,
        specs: dict[str, Any],
        capabilities: dict[str, ToolCapability],
        groups: dict[str, str] | None = None,
    ) -> None:
        """复制可信注册资料；非法名称、能力、分组或无法序列化的说明抛 ValueError。"""
        validate_groups(groups or {}, specs)
        if capabilities.keys() - specs.keys() or any(
            not isinstance(value, ToolCapability) for value in capabilities.values()
        ):
            raise ValueError("工具能力与目录未正确配对。")
        self._entries: dict[str, ToolListing] = {}
        self._schemas: dict[str, str] = {}
        for name, spec in specs.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) is None
                or not isinstance(spec, dict)
                or "name" in spec
                or not isinstance(spec.get("description", ""), str)
            ):
                raise ValueError("工具名称或说明无效。")
            description = " ".join(spec.get("description", "").split())
            summary = description if len(description) <= 160 else description[:159] + "…"
            self._entries[name] = ToolListing(
                name,
                (groups or {}).get(name, "extensions"),
                summary,
                capabilities.get(name, ToolCapability.UNKNOWN),
            )
            try:
                encoded = json.dumps(
                    {"type": "function", "function": {"name": name, **spec}},
                    ensure_ascii=False,
                    allow_nan=False,
                )
                encoded.encode("utf-8")
            except (TypeError, ValueError) as error:
                raise ValueError("工具说明必须是可编码的有限 JSON。") from error
            self._schemas[name] = encoded
        # 摘要包括完整 Schema、分组与能力；参数变化也不能继续使用旧目录索引。
        fingerprint = sha256()
        for name, entry in self._entries.items():
            fingerprint.update(
                json.dumps([self._schemas[name], entry.group, entry.capability.value]).encode()
            )
        self.version = fingerprint.hexdigest()

    def listings(self, mode: ExecutionMode) -> tuple[ToolListing, ...]:
        """按可信能力过滤目录；沿用执行器相同的模式规则，不检查具体参数的路径权限。"""
        if not isinstance(mode, ExecutionMode):
            raise ValueError("目录模式必须使用 ExecutionMode。")
        result = []
        for entry in self._entries.values():
            try:
                check_tool_mode(mode, entry.capability)
            except ToolModeError:
                continue
            result.append(entry)
        return tuple(result)

    def snapshot(self, names: Collection[str], mode: ExecutionMode) -> ToolExposure:
        """返回按注册顺序去重的完整说明；未知或模式禁止的名称整体拒绝。"""
        selected = set(names)
        allowed = {entry.name for entry in self.listings(mode)}
        if selected - allowed:
            raise ToolError("选择包含未知或当前模式不可用的工具。")
        ordered = tuple(name for name in self._entries if name in selected)
        return ToolExposure(ordered, tuple(self._schemas[name] for name in ordered))


class ToolVisibility:
    """单轮可变选择；每轮创建新实例，预览使用独立实例，不共享跨会话选择。"""

    def __init__(
        self,
        directory: ToolDirectory,
        mode: ExecutionMode,
        loading: ToolLoadingMode = ToolLoadingMode.ON_DEMAND,
    ) -> None:
        if not isinstance(loading, ToolLoadingMode):
            raise ValueError("工具加载方式必须使用 ToolLoadingMode。")
        self._directory, self._mode = directory, mode
        self._entries = directory.listings(mode)
        self._selected = (
            {entry.name for entry in self._entries} if loading is ToolLoadingMode.FULL else set()
        )

    def listings(self) -> tuple[ToolListing, ...]:
        """返回本轮可发现的简短目录；不改变选择，也不展开参数。"""
        return self._entries

    def snapshot(self) -> ToolExposure:
        """只读取得当前选择；旧快照不会被后来的发现或调用者修改影响。"""
        return self._directory.snapshot(self._selected, self._mode)

    def discover(
        self,
        *,
        names: tuple[str, ...] = (),
        groups: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """按准确名称或整组加入本轮选择，返回本次新增名称，重复选择返回空元组。

        先校验整批再修改；未知、模式禁止或空选择抛 ToolError，原状态保留。
        选择不表示已经展示给模型，不授予文件/进程权限，也不执行处理函数。
        """
        if (
            not isinstance(names, tuple)
            or not isinstance(groups, tuple)
            or (not names and not groups)
            or any(not isinstance(value, str) or not value for value in (*names, *groups))
        ):
            raise ToolError("发现工具需要非空的名称或分组列表。")
        available = {entry.name for entry in self._entries}
        available_groups = {entry.group for entry in self._entries}
        if set(names) - available or set(groups) - available_groups:
            raise ToolError("选择包含未知或当前模式不可用的工具/分组。")
        wanted = set(names) | {entry.name for entry in self._entries if entry.group in groups}
        added = tuple(
            entry.name
            for entry in self._entries
            if entry.name in wanted and entry.name not in self._selected
        )
        self._selected.update(wanted)
        return added
