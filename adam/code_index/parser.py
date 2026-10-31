"""只解析 Python 3.11 语法，提取定义、导入和名字使用；不导入、编译执行或求值源码。"""

import ast
import codecs
import tokenize
from collections.abc import Callable
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from adam.code_index.bindings import BindingCollector, ReferenceLimitError
from adam.code_index.models import CodeIssue, CodeLimits, CodeModule, Definition, ImportBinding


class _ParseLimitError(Exception):
    """当前语法树超限，整份文件只返回诊断而不提供不完整的定义列表。"""


def module_name(relative: Path) -> str:
    """以索引根的相对路径生成模块标签；这是结构标签，不模拟 sys.path 或包加载。"""
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "<root>"


def parse_module(
    path: Path,
    relative: Path,
    raw: bytes,
    limits: CodeLimits,
    check_cancel: Callable[[], None],
) -> CodeModule:
    """从同一份有界 UTF-8 字节生成结构；语法/编码/树大小错误返回无旧符号的诊断。

    path 仅用于来源标签，函数本身不打开文件；取消和宿主扫描超时仍向上传播。
    parsed 只说明 AST 可构造，不替代编译、类型检查或运行验证。
    """
    name = module_name(relative)
    if len(raw) > limits.max_file_bytes:
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            None,
            issue=CodeIssue("file_too_large", "文件超过单文件字节上限，未建立索引。"),
        )
    version = sha256(raw).hexdigest()
    check_cancel()
    try:
        text = _decode_source(raw)
        tree = ast.parse(text, filename=str(path), mode="exec", feature_version=(3, 11))
    except UnicodeError:
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            version,
            issue=CodeIssue("encoding_error", "只支持 UTF-8 Python 源文件。"),
        )
    except (SyntaxError, ValueError) as error:
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            version,
            issue=CodeIssue(
                "syntax_error",
                error.msg[:200] if isinstance(error, SyntaxError) else "源码包含无效字符。",
                error.lineno if isinstance(error, SyntaxError) else None,
            ),
        )
    except (RecursionError, MemoryError):
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            version,
            issue=CodeIssue("resource_limit", "解析资源不足，未建立该文件索引。"),
        )
    check_cancel()
    try:
        _check_tree(tree, limits, check_cancel)
        definitions, imports = _extract(path, name, version, tree, text, limits, check_cancel)
        scopes, uses = BindingCollector(definitions, limits.max_references, check_cancel).collect(
            tree
        )
    except (RecursionError, MemoryError):
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            version,
            issue=CodeIssue("resource_limit", "结构或引用遍历资源不足，未建立该文件索引。"),
        )
    except (_ParseLimitError, ReferenceLimitError) as error:
        return CodeModule(
            str(path),
            relative.as_posix(),
            name,
            version,
            issue=CodeIssue("resource_limit", str(error)),
        )
    return CodeModule(
        str(path),
        relative.as_posix(),
        name,
        version,
        tuple(definitions),
        tuple(imports),
        scopes=scopes,
        uses=uses,
    )


def _decode_source(raw: bytes) -> str:
    """同时核对源码编码声明与 BOM，避免按 UTF-8 强读出另一套名字含义。"""
    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
        if codecs.lookup(encoding).name not in {"utf-8", "utf-8-sig"}:
            raise UnicodeError("unsupported Python source encoding")
    except (SyntaxError, LookupError) as error:
        raise UnicodeError("invalid Python source encoding") from error
    return raw.decode("utf-8-sig")


def _check_tree(tree: ast.AST, limits: CodeLimits, check_cancel: Callable[[], None]) -> None:
    """迭代检查深度和节点数，避免在有界源码上再进行无界递归遍历。"""
    stack = [(tree, 0)]
    count = 0
    while stack:
        check_cancel()
        node, depth = stack.pop()
        count += 1
        if count > limits.max_ast_nodes or depth > limits.max_ast_depth:
            raise _ParseLimitError("语法树节点数或深度超限，未建立该文件索引。")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def _summary(node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """结构预览只保留文档字符串的首行摘要，不把正文当作执行指导。"""
    text = ast.get_docstring(node) or ""
    return text.splitlines()[0][:200] if text else ""


def _extract(
    path: Path,
    module: str,
    version: str,
    tree: ast.Module,
    text: str,
    limits: CodeLimits,
    check_cancel: Callable[[], None],
) -> tuple[list[Definition], list[ImportBinding]]:
    """函数与类建立新作用域，if/try 等控制块沿用原作用域；重复定义按位置保留。"""
    root_id = f"{path.as_posix()}::{module}@1:0:module"
    definitions = [
        Definition(
            root_id,
            str(path),
            module,
            version,
            module,
            module,
            "module",
            None,
            1,
            max(1, len(text.splitlines())),
            _summary(tree),
        )
    ]
    imports: list[ImportBinding] = []
    stack: list[tuple[ast.AST, str, str]] = [
        (node, root_id, module) for node in reversed(tree.body)
    ]
    while stack:
        check_cancel()
        node, scope, prefix = stack.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = f"{prefix}.{node.name}"
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            identifier = f"{path.as_posix()}::{qualified}@{node.lineno}:{node.col_offset}:{kind}"
            definitions.append(
                Definition(
                    identifier,
                    str(path),
                    module,
                    version,
                    node.name,
                    qualified,
                    kind,
                    scope,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    _summary(node),
                )
            )
            # 定义参数、装饰器和基类表达式中没有命名定义或 import 语句；仅进入定义体。
            stack.extend((child, identifier, qualified) for child in reversed(node.body))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imports.append(
                    ImportBinding(
                        str(path),
                        module,
                        version,
                        scope,
                        "import" if isinstance(node, ast.Import) else "from",
                        alias.asname
                        or (
                            alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                        ),
                        alias.name if isinstance(node, ast.Import) else node.module or "",
                        None if isinstance(node, ast.Import) else alias.name,
                        0 if isinstance(node, ast.Import) else node.level,
                        alias.lineno,
                        alias.end_lineno or alias.lineno,
                        aliased=alias.asname is not None,
                    )
                )
        else:
            stack.extend(
                (child, scope, prefix) for child in reversed(list(ast.iter_child_nodes(node)))
            )
        if len(definitions) + len(imports) > limits.max_symbols:
            raise _ParseLimitError("文件中的定义和导入数量超限，未返回部分索引。")
    return definitions, imports
