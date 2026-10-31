"""收集静态名字使用与遮蔽；复杂动态作用域保留未知，不执行或求值源码。"""

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from adam.code_index.models import CodeScope, Definition, NameUse


class ReferenceLimitError(ValueError):
    """本文件的名字使用数量超限，调用方应舍弃整份解析结果。"""


@dataclass
class _Scope:
    identifier: str
    parent_id: str | None
    kind: str
    blocked: set[str] = field(default_factory=set)


class BindingCollector(ast.NodeVisitor):
    """从已验证深度的 AST 收集作用域；赋值、控制分支和动态声明不推断值。"""

    def __init__(
        self, definitions: list[Definition], limit: int, check_cancel: Callable[[], None]
    ) -> None:

        self.lookup = {item.identifier: item for item in definitions}
        self.scopes = {
            item.identifier: _Scope(item.identifier, item.scope_id, item.kind)
            for item in definitions
        }
        self.current = self.scopes[definitions[0].identifier]
        self.positions = {
            (item.start_line, item.qualified_name, item.kind): item for item in definitions
        }
        self.uses: list[NameUse] = []
        self.dynamic_names: set[str] = set()
        self.has_star = False
        self.control_depth = 0
        self.unsupported = False
        self.limit = limit
        self.check = check_cancel

    def collect(self, tree: ast.Module) -> tuple[tuple[CodeScope, ...], tuple[NameUse, ...]]:
        """返回与同次 AST 对应的不可变元数据；取消和数量超限向上传播。"""
        self.visit(tree)
        scopes = tuple(
            CodeScope(
                scope.identifier,
                scope.parent_id,
                scope.kind,
                tuple(sorted(scope.blocked | self.dynamic_names)),
                self.has_star,
            )
            for scope in self.scopes.values()
        )
        return scopes, tuple(self.uses)

    def visit(self, node: ast.AST) -> None:
        self.check()
        super().visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        controlled = isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.TryStar,
                ast.With,
                ast.AsyncWith,
                ast.Match,
            ),
        )
        previous = self.control_depth
        if controlled:
            self.control_depth += 1
        super().generic_visit(node)
        self.control_depth = previous

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._use(node, (node.id,))
        elif not self.unsupported:
            self.current.blocked.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = [node.attr]
        root = node.value
        while isinstance(root, ast.Attribute):
            parts.insert(0, root.attr)
            root = root.value
        if isinstance(root, ast.Name) and isinstance(node.ctx, ast.Load):
            self._use(node, (root.id, *parts))
        else:
            if isinstance(root, ast.Name) and not isinstance(node.ctx, ast.Load):
                self.current.blocked.add(root.id)
            # 实例/调用结果的属性无法映射到定义；仍收集用于求出对象的独立名字。
            if isinstance(node.ctx, ast.Load):
                self._use(node, tuple(parts), "dynamic_attribute")
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node, "async_function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node, "class")

    def _definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str
    ) -> None:
        """默认值、装饰器和基类属于外层；定义体才进入新的作用域。"""
        outer, control = self.current, self.control_depth
        if control:
            outer.blocked.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for expression in [*node.bases, *node.keywords]:
                self.visit(expression)
        else:
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for argument in self._arguments(node.args):
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if node.returns is not None:
                self.visit(node.returns)
        qualified = self.lookup[outer.identifier].qualified_name + "." + node.name
        definition = self.positions[(node.lineno, qualified, kind)]
        self.current = self.scopes[definition.identifier]
        if not isinstance(node, ast.ClassDef):
            self.current.blocked.update(arg.arg for arg in self._arguments(node.args))
        self.control_depth = 0
        for statement in node.body:
            self.visit(statement)
        self.current, self.control_depth = outer, control

    @staticmethod
    def _arguments(arguments: ast.arguments) -> list[ast.arg]:
        return [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ]

    def visit_Import(self, node: ast.Import) -> None:
        if self.control_depth:
            self.current.blocked.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" for alias in node.names):
            self.has_star = True
        if self.control_depth:
            self.current.blocked.update(alias.asname or alias.name for alias in node.names)

    def visit_Global(self, node: ast.Global) -> None:
        # 全局/非局部写入可以来自另一函数；本版对整个文件同名引用保守降为未知。
        self.dynamic_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.dynamic_names.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.current.blocked.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.current.blocked.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.current.blocked.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.current.blocked.add(node.rest)
        self.generic_visit(node)

    def _unsupported_scope(self, node: ast.AST) -> None:
        """lambda/推导式单列未知；其中海象绑定可能影响外层，禁止外层误认旧定义。"""
        previous = self.unsupported
        self.unsupported = True
        for child in ast.walk(node):
            self.check()
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self.dynamic_names.add(child.target.id)
        self.generic_visit(node)
        self.unsupported = previous

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._unsupported_scope(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._unsupported_scope(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._unsupported_scope(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._unsupported_scope(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._unsupported_scope(node)

    def _use(self, node: ast.expr, parts: tuple[str, ...], reason: str = "") -> None:
        if len(self.uses) >= self.limit:
            raise ReferenceLimitError("文件的名字使用数量超限，未返回部分索引。")
        reason = reason or ("unsupported_scope" if self.unsupported else "")
        self.uses.append(
            NameUse(
                self.current.identifier,
                parts,
                node.lineno,
                node.end_lineno or node.lineno,
                node.col_offset,
                reason,
            )
        )
