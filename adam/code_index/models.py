"""Python 结构索引的不可变结果和资源限制；路径权限由宿主注入，不执行目标模块。"""

import math
from dataclasses import dataclass
from typing import Literal

from adam.contracts.errors import ToolError


class CodeIndexError(ToolError):
    """本次索引不能可靠完成；调用方不得用上次缓存冒充当前结果。"""


@dataclass(frozen=True)
class CodeLimits:
    """单次索引资源上限；字节限制针对原文件，超时为扫描和解析边界的协作检查。"""

    max_scan_entries: int = 5000
    max_files: int = 512
    max_file_bytes: int = 1048576
    max_total_bytes: int = 16777216
    max_ast_nodes: int = 100000
    max_ast_depth: int = 100
    max_symbols: int = 20000
    max_references: int = 100000
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """拒绝布尔计数、无穷超时和不合理上限，直接调用与后续工具组装沿用同一约束。"""
        for name, maximum in (
            ("max_scan_entries", 1000000),
            ("max_files", 10000),
            ("max_file_bytes", 16777216),
            ("max_total_bytes", 268435456),
            ("max_ast_nodes", 1000000),
            ("max_ast_depth", 200),
            ("max_symbols", 100000),
            ("max_references", 1000000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes exceeds max_total_bytes")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 300")


@dataclass(frozen=True)
class CodeIssue:
    """当前文件未形成结构结果的原因；line 为可确定的首个错误行，否则保持未知。"""

    reason: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class Definition:
    """有来源版本的语法定义；scope_id 指向语法父定义，不表示运行时对象身份。"""

    identifier: str
    path: str
    module: str
    version: str
    name: str
    qualified_name: str
    kind: Literal["module", "class", "function", "async_function"]
    scope_id: str | None
    start_line: int
    end_line: int
    summary: str = ""


@dataclass(frozen=True)
class ImportBinding:
    """一条显式导入绑定；相对层数和别名原样保留，尚未执行导入或推断重导出。"""

    path: str
    module: str
    version: str
    scope_id: str
    kind: Literal["import", "from"]
    local_name: str
    target_module: str
    imported_name: str | None
    level: int
    start_line: int
    end_line: int
    aliased: bool = False


@dataclass(frozen=True)
class CodeScope:
    """名字查询范围；blocked_names 表示赋值/参数等遮蔽，wildcard 表示有星号导入。"""

    identifier: str
    parent_id: str | None
    kind: str
    blocked_names: tuple[str, ...] = ()
    wildcard: bool = False


@dataclass(frozen=True)
class NameUse:
    """一次名字或属性链的语法使用；列为从零开始的 UTF-8 字节位置。"""

    scope_id: str
    parts: tuple[str, ...]
    start_line: int
    end_line: int
    column: int
    reason: str = ""


@dataclass(frozen=True)
class CodeReference:
    """有源版本的引用判断；definition_id 未知时必须保留 reason，不伪造目标。"""

    path: str
    version: str
    expression: str
    start_line: int
    end_line: int
    column: int
    definition_id: str | None
    reason: str


@dataclass(frozen=True)
class CodeModule:
    """同一次字节读取的结构或诊断；未完整读取时 version 为 None，失败不保留旧定义。"""

    path: str
    relative_path: str
    module: str
    version: str | None
    definitions: tuple[Definition, ...] = ()
    imports: tuple[ImportBinding, ...] = ()
    issue: CodeIssue | None = None
    scopes: tuple[CodeScope, ...] = ()
    uses: tuple[NameUse, ...] = ()


@dataclass(frozen=True)
class CodeSnapshot:
    """一次完整扫描的当前快照；文件诊断可并存，范围超限或取消不产生快照。"""

    root: str
    modules: tuple[CodeModule, ...]
    generation: str
    skipped_entries: int = 0

    def find_definitions(self, name: str) -> tuple[Definition, ...]:
        """按短名、限定名或 ID 返回全部匹配；不同文件或作用域的同名定义不会合并。"""
        return tuple(
            definition
            for module in self.modules
            for definition in module.definitions
            if name in {definition.name, definition.qualified_name, definition.identifier}
        )
