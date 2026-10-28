"""保护分层边界，防止后续功能再次让界面和配置直接引用具体执行实现。"""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module,forbidden",
    [
        ("cli", {"httpx", "adam.llm", "adam.storage", "adam.conversation", "adam.tool_calls"}),
        ("config", {"httpx", "adam.llm", "adam.storage", "adam.tool_calls"}),
        (
            "conversation",
            {"httpx", "adam.llm", "adam.config", "adam.tool_calls.factory", "adam.cli"},
        ),
        (
            "tool_calls.registry",
            {
                "adam.tool_calls.command_tools",
                "adam.tool_calls.file_tools",
                "adam.tool_calls.git_tools",
                "adam.tool_calls.policy",
                "adam.cli",
            },
        ),
        (
            "settings",
            {"httpx", "adam.config", "adam.llm", "adam.storage", "adam.tool_calls", "adam.cli"},
        ),
    ],
)
def test_layer_does_not_import_forbidden_implementation(module, forbidden):
    source = Path(__file__).resolve().parents[1] / "adam" / (module.replace(".", "/") + ".py")
    imports = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not {
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    }
