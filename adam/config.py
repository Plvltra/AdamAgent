"""Read and validate the local TOML configuration."""

import math
import os
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from adam.tool_calls import PermissionConfig, ToolLimits


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120


def checked_section(value: object, allowed: Collection[str], name: str) -> dict[str, Any]:
    """校验配置表及未知字段，返回原表；错误包含字段名但不回显配置值。"""
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是配置表。")
    unknown = value.keys() - set(allowed)
    if unknown:
        raise ConfigError(f"[{name}] 存在不支持的配置项：{', '.join(sorted(unknown))}。")
    return value


def load_llm_config(path: Path) -> LLMConfig:
    try:
        # utf-8-sig also accepts files saved with a Windows UTF-8 BOM.
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"无法读取模型配置 {path}，请检查文件存在且为有效 UTF-8 TOML。") from None

    checked_section(data, {"llm"}, "模型配置")

    section = data.get("llm")
    if not isinstance(section, dict):
        raise ConfigError("配置缺少 [llm] 表。")

    allowed = {"base_url", "model", "api_key", "api_key_env", "timeout_seconds"}
    checked_section(section, allowed, "llm")

    def required_text(name: str) -> str:
        value = section.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"llm.{name} 必须是非空字符串。")
        return value.strip()

    base_url = required_text("base_url").rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
        )
        # Accessing port also validates its numeric value and range.
        _ = url.port
    except ValueError:
        valid = False

    if not valid:
        raise ConfigError("base_url 必须是无凭据、查询参数和片段的 HTTP(S) 地址。")

    model = required_text("model")

    if ("api_key" in section) == ("api_key_env" in section):
        raise ConfigError("api_key 与 api_key_env 必须且只能配置一个。")

    if "api_key" in section:
        key = required_text("api_key")
    else:
        key = os.environ.get(required_text("api_key_env"), "").strip()
        if not key:
            raise ConfigError("api_key_env 指定的环境变量未设置或为空。")

    if not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ConfigError("API 密钥必须是无空白的可打印 ASCII 字符串。")

    timeout = section.get("timeout_seconds", 120)
    # bool is an int subclass, but is not a valid timeout.
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout_seconds 必须是有限的正数。")

    return LLMConfig(
        base_url=base_url,
        model=model,
        api_key=key,
        timeout_seconds=float(timeout),
    )


@dataclass(frozen=True)
class AgentConfig:
    max_model_requests: int = 8
    max_tool_calls: int = 16
    max_session_tokens: int = 50000
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    sessions_dir: Path
    system_prompt: str
    agent: AgentConfig = field(default_factory=AgentConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    tool_limits: ToolLimits = field(default_factory=ToolLimits)


def load_config(path: Path, llm_path: Path | None = None) -> AppConfig:
    """加载主配置；相对存储路径和默认模型配置以主配置目录为基准。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"无法读取主配置 {path}，请检查文件存在且为有效 UTF-8 TOML。") from None

    checked_section(data, {"storage", "chat", "agent", "permissions", "tool_limits"}, "主配置")
    storage = data.get("storage", {})
    if isinstance(storage, dict) and "db_path" in storage:
        raise ConfigError("storage.db_path 已移除，请改用 storage.sessions_dir。")
    storage = checked_section(storage, {"sessions_dir"}, "storage")
    chat = checked_section(data.get("chat", {}), {"system_prompt"}, "chat")

    directory_value = storage.get("sessions_dir", "sessions")
    if (
        not isinstance(directory_value, str)
        or not directory_value.strip()
        or "\0" in directory_value
    ):
        raise ConfigError("storage.sessions_dir 必须是非空且不含空字符的路径字符串。")

    prompt = chat.get("system_prompt", "你是 Adam，一个友好的助手。")
    if not isinstance(prompt, str):
        raise ConfigError("chat.system_prompt 必须是字符串。")

    directory = path.resolve().parent
    sessions_dir = Path(directory_value)
    if not sessions_dir.is_absolute():
        sessions_dir = directory / sessions_dir

    model_config_path = llm_path if llm_path is not None else directory / "llm.toml"
    llm = load_llm_config(model_config_path)

    agent_data = checked_section(
        data.get("agent", {}), {item.name for item in fields(AgentConfig)}, "agent"
    )
    limit_data = checked_section(
        data.get("tool_limits", {}), {item.name for item in fields(ToolLimits)}, "tool_limits"
    )
    permission_data = checked_section(
        data.get("permissions", {}),
        {item.name for item in fields(PermissionConfig)},
        "permissions",
    )
    for section in (agent_data, limit_data):
        for key, value in section.items():
            if type(value) is not int or not 0 < value <= 100000000:
                raise ConfigError(f"{key} 必须是合理范围内的正整数。")
    roots = permission_data.get("additional_read_dirs", [])
    if not isinstance(roots, list) or any(
        not isinstance(root, str) or not root.strip() or "\0" in root for root in roots
    ):
        raise ConfigError("permissions.additional_read_dirs 必须是非空路径字符串数组。")
    resolved_roots = []
    for root in roots:
        target = Path(root).expanduser()
        resolved_roots.append(
            (directory / target).resolve() if not target.is_absolute() else target.resolve()
        )
    if limit_data.get("max_result_chars", 8000) < 512:
        raise ConfigError("tool_limits.max_result_chars 至少为512。")
    return AppConfig(
        llm=llm,
        sessions_dir=sessions_dir,
        system_prompt=prompt,
        agent=AgentConfig(**agent_data),
        permissions=PermissionConfig(additional_read_dirs=tuple(resolved_roots)),
        tool_limits=ToolLimits(**limit_data),
    )


def adam_home() -> Path:
    """默认用户数据根；ADAM_HOME 可覆盖，空值回退默认目录。"""
    value = os.environ.get("ADAM_HOME", "").strip()
    return Path(value).expanduser().resolve() if value else Path.home() / ".adam"
