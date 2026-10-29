"""保护包的依赖方向及关键边界；新增文件也必须遵守所属模块的职责。"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "adam"


def imported_modules(source):
    """收集源码里的静态导入；函数内延迟导入也不能绕开模块边界。"""
    imports = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "adam":
                imports.update(f"adam.{alias.name}" for alias in node.names)
            else:
                imports.add(node.module)
    return imports


@pytest.mark.parametrize(
    "module,forbidden",
    [
        pytest.param(
            "cli.main",
            {
                "httpx",
                "adam.llm",
                "adam.session",
                "adam.application.conversation",
                "adam.tool_calls",
            },
            id="cli-forbidden0",
        ),
        pytest.param(
            "config.loader",
            {"httpx", "adam.llm", "adam.session", "adam.tool_calls"},
            id="config-forbidden1",
        ),
        pytest.param(
            "application.conversation",
            {"httpx", "adam.llm", "adam.config.loader", "adam.tool_calls.factory", "adam.cli"},
            id="conversation-forbidden2",
        ),
        pytest.param(
            "tool_calls.registry",
            {
                "adam.tool_calls.command_tools",
                "adam.tool_calls.edit_tools",
                "adam.tool_calls.file_tools",
                "adam.tool_calls.git_tools",
                "adam.tool_calls.policy",
                "adam.cli",
            },
            id="tool_calls.registry-forbidden3",
        ),
        pytest.param(
            "config.settings",
            {
                "httpx",
                "adam.config.loader",
                "adam.llm",
                "adam.session",
                "adam.tool_calls",
                "adam.cli",
            },
            id="settings-forbidden4",
        ),
    ],
)
def test_layer_does_not_import_forbidden_implementation(module, forbidden):
    imports = imported_modules(ROOT / (module.replace(".", "/") + ".py"))
    assert not {
        name
        for name in imports
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    }


@pytest.mark.parametrize(
    "package,allowed",
    [
        ("contracts", {"contracts"}),
        ("config", {"config", "contracts"}),
        ("session", {"session", "contracts"}),
        ("memory", {"memory", "contracts"}),
        ("llm", {"llm", "config", "contracts"}),
        ("context", {"context", "session", "config", "contracts"}),
        ("tool_calls", {"tool_calls", "session", "config", "contracts"}),
        ("application", {"application", "context", "session", "tool_calls", "config", "contracts"}),
        ("cli", {"cli", "application", "context", "config", "contracts", "bootstrap"}),
    ],
)
def test_package_dependency_direction(package, allowed):
    """检查整包，防止新增文件反向依赖界面、组装入口或不属于它的执行实现。"""
    violations = []
    for source in (ROOT / package).rglob("*.py"):
        for module in imported_modules(source):
            if module.startswith("adam.") and module.split(".")[1] not in allowed:
                violations.append(f"{source.relative_to(ROOT)} -> {module}")
            if module == "adam.bootstrap" and source != ROOT / "cli" / "main.py":
                violations.append(f"{source.relative_to(ROOT)} cannot assemble dependencies")
    assert not violations, "\n".join(violations)
