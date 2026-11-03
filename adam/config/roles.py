"""宿主显式加载角色 TOML；角色只能收窄模型与工具上限，不能授予文件权限。"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from adam.contracts.errors import ConfigError
from adam.contracts.execution import ToolCapability


@dataclass(frozen=True)
class RoleProfile:
    """调查者只读，执行者可请求编辑/命令；实际路径、模式和审批仍由原工具决定。"""

    name: str
    model: str
    tools: frozenset[str]
    max_model_requests: int

    @property
    def capabilities(self) -> frozenset[ToolCapability]:
        base = {ToolCapability.READ_ONLY, ToolCapability.SESSION_STATE}
        if self.name == "executor":
            base.update({ToolCapability.EDIT_FILE, ToolCapability.RUN_PROCESS})
        return frozenset(base)


def load_roles(path: Path, models: frozenset[str], max_requests: int) -> dict[str, RoleProfile]:
    """读取明确指定的本地配置；模型必须在宿主目录内，请求数不得超过宿主上限。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        if set(data) != {"roles"} or set(data["roles"]) != {"investigator", "executor"}:
            raise ValueError("expected two role definitions")
        profiles = {}
        for name, item in data["roles"].items():
            if set(item) != {"model", "tools", "max_model_requests"}:
                raise ValueError("invalid role fields")
            model, tools, requests = item["model"], item["tools"], item["max_model_requests"]
            if not isinstance(model, str) or model not in models:
                raise ValueError("model outside host directory")
            if (
                not isinstance(tools, list)
                or not tools
                or any(not isinstance(tool, str) or not tool for tool in tools)
            ):
                raise ValueError("invalid tool names")
            if (
                len(set(tools)) != len(tools)
                or type(requests) is not int
                or not 1 <= requests <= max_requests
            ):
                raise ValueError("invalid role bounds")
            profiles[name] = RoleProfile(name, model, frozenset(tools), requests)
        return profiles
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ConfigError(
            "角色配置无效：需要调查者/执行者、已允许的模型和不超过宿主的请求上限。"
        ) from error
