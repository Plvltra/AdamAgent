"""在已授权快照内解析保守的静态名字绑定，不模拟安装环境或运行时对象。"""

from collections import defaultdict
from collections.abc import Callable, Iterator

from adam.code_index.models import (
    CodeModule,
    CodeReference,
    CodeScope,
    CodeSnapshot,
    Definition,
    ImportBinding,
    NameUse,
)


class ReferenceIndex:
    """解析唯一词法定义和索引内导入；重绑定、动态属性和不支持的作用域保持未知。"""

    def __init__(self, snapshot: CodeSnapshot, check_cancel: Callable[[], None]) -> None:
        self.snapshot = snapshot
        self.check = check_cancel
        self.scopes: dict[str, CodeScope] = {}
        self.owners: dict[str, CodeModule] = {}
        self.bindings: dict[tuple[str, str], list[Definition | ImportBinding]] = defaultdict(list)
        self.modules: dict[str, list[CodeModule]] = defaultdict(list)
        for module in snapshot.modules:
            self.check()
            self.modules[module.module].append(module)
            for scope in module.scopes:
                self.scopes[scope.identifier] = scope
                self.owners[scope.identifier] = module
            for definition in module.definitions:
                if definition.scope_id is not None:
                    self.bindings[(definition.scope_id, definition.name)].append(definition)
            for binding in module.imports:
                self.bindings[(binding.scope_id, binding.local_name)].append(binding)

    def references(self) -> Iterator[CodeReference]:
        """逐个返回带源版本的静态引用或未知原因；不把语法绑定当作实际调用成功的证据。"""
        for module in self.snapshot.modules:
            for use in module.uses:
                self.check()
                target, reason = self._resolve(use)
                yield CodeReference(
                    module.path,
                    module.version or "",
                    ".".join(use.parts),
                    use.start_line,
                    use.end_line,
                    use.column,
                    target.identifier if target else None,
                    reason,
                )

    def _resolve(self, use: NameUse) -> tuple[Definition | None, str]:
        if use.reason:
            return None, use.reason
        scope = self.scopes[use.scope_id]
        if scope.kind == "class":
            return None, "class_namespace"
        while True:
            self.check()
            if scope.wildcard or use.parts[0] in scope.blocked_names:
                return None, "dynamic_binding"
            if (scope.identifier, use.parts[0]) in self.bindings:
                return self._binding(scope, use.parts[0], use.parts[1:], set())
            parent = scope.parent_id
            # 类体不是方法的闭包作用域；嵌套函数可跳过类继续查外层函数。
            while parent is not None and self.scopes[parent].kind == "class":
                parent = self.scopes[parent].parent_id
            if parent is None:
                return None, "external_or_dynamic"
            scope = self.scopes[parent]

    def _binding(
        self,
        scope: CodeScope,
        name: str,
        attributes: tuple[str, ...],
        seen: set[tuple[str, str]],
    ) -> tuple[Definition | None, str]:
        """沿唯一导入绑定解析，环路和多候选均不猜测；深度上限独立于文件数量。"""
        self.check()
        key = (scope.identifier, name)
        if key in seen or len(seen) >= 32:
            return None, "import_cycle_or_depth"
        if scope.wildcard or name in scope.blocked_names:
            return None, "dynamic_binding"
        values = self.bindings.get(key, [])
        if len(values) != 1:
            return None, "ambiguous_binding" if values else "missing_binding"
        value = values[0]
        if isinstance(value, Definition):
            if attributes:
                return None, "dynamic_attribute"
            return value, "static_binding"
        seen = seen | {key}
        if value.kind == "import":
            if not value.aliased:
                suffix = tuple(value.target_module.split(".")[1:])
                if attributes[: len(suffix)] != suffix:
                    return None, "unresolved_import_prefix"
                attributes = attributes[len(suffix) :]
            return self._module(value.target_module, attributes, seen)
        owner = self.owners[scope.identifier]
        module_name = value.target_module
        if value.level:
            package = (
                owner.module
                if owner.relative_path.rsplit("/", 1)[-1] == "__init__.py"
                else owner.module.rpartition(".")[0]
            )
            parts = package.split(".") if package and package != "<root>" else []
            if len(parts) < value.level:
                return None, "relative_import_outside_index"
            module_name = ".".join(
                parts[: len(parts) - value.level + 1] + ([module_name] if module_name else [])
            )
        return self._module(module_name, (value.imported_name or "", *attributes), seen)

    def _module(
        self, name: str, attributes: tuple[str, ...], seen: set[tuple[str, str]]
    ) -> tuple[Definition | None, str]:
        """只在扫描范围内连接模块；缺少包文件时允许已索引的命名空间子模块。"""
        self.check()
        parts = name.split(".")
        for length in range(1, len(parts)):
            parents = self.modules.get(".".join(parts[:length]), [])
            if len(parents) > 1 or any(
                parent.relative_path.rsplit("/", 1)[-1] != "__init__.py" for parent in parents
            ):
                return None, "ambiguous_package"
        modules = self.modules.get(name, [])
        if len(modules) > 1:
            return None, "ambiguous_module"
        if len(modules) == 1:
            module = modules[0]
            if module.issue or not module.scopes:
                return None, "module_unavailable"
            root = module.scopes[0]
            if not attributes:
                return module.definitions[0], "static_binding"
            if root.wildcard or attributes[0] in root.blocked_names:
                return None, "dynamic_binding"
            if (root.identifier, attributes[0]) in self.bindings:
                return self._binding(root, attributes[0], attributes[1:], seen)
        if attributes and len(seen) < 32:
            child = ".".join((name, attributes[0])) if name else attributes[0]
            if child in self.modules or any(key.startswith(child + ".") for key in self.modules):
                return self._module(child, attributes[1:], seen)
        return None, "external_or_dynamic"
